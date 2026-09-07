"""Read-only access to the claims database for the ``query_claims_db`` tool.

Defense in depth for policyholder PII: the model is told not to select PII
columns, but the real guard is SQLite's authorizer callback, which denies any
read of ``policyholders.name/cpf/phone/email/birth_date`` and any action other
than SELECT/READ/FUNCTION. Statements are single, capped at ``row_limit`` rows
and aborted after ``timeout_s`` through a progress handler.
"""
from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

PII_COLUMNS = {"name", "cpf", "phone", "email", "birth_date"}
PII_TABLE = "policyholders"
FORBIDDEN_RE = re.compile(r"\b(attach|detach|pragma|vacuum|load_extension|sqlite_master|sqlite_temp_master)\b", re.I)

SCHEMA_SUMMARY = """\
policyholders(policyholder_id, city, state) -- colunas name, cpf, phone, email, birth_date são PII: PROIBIDO selecionar
policies(policy_id, policy_number, policyholder_id, product ['Auto','Residencial','Empresarial'], start_date, end_date, annual_premium, status ['Ativa','Vencida','Cancelada'])
claims(claim_id, claim_number 'SIN-AAAA-NNNNNN', policy_id, claim_type, occurrence_date, notice_date, registration_date, status ['Aberto','Em regulação','Pago','Pago parcial','Negado'], claim_amount = VALOR REIVINDICADO pelo segurado (NÃO é o valor pago), description)
payments(payment_id, claim_id, payment_date, paid_amount = VALOR EFETIVAMENTE PAGO, payment_type ['Integral','Parcial'], payment_status)
Datas em texto 'YYYY-MM-DD'. Sinistro 'Pago' tem exatamente um pagamento; 'Aberto', 'Em regulação' e 'Negado' não têm pagamento."""


class SQLGuardError(Exception):
    """Raised when a statement is rejected or fails; message is safe to show to the model."""


@dataclass
class QueryResult:
    sql: str
    columns: list[str]
    rows: list[list[object]]
    row_count: int
    truncated: bool
    elapsed_ms: int
    snapshot: str
    notes: list[str] = field(default_factory=list)

    def as_text(self, max_rows: int = 20) -> str:
        lines = [" | ".join(self.columns)]
        for r in self.rows[:max_rows]:
            lines.append(" | ".join(_fmt(v) for v in r))
        if self.row_count > max_rows:
            lines.append(f"... ({self.row_count} linhas no total, exibindo {max_rows})")
        if self.truncated:
            lines.append("(resultado truncado pelo limite de linhas; use agregações ou filtros)")
        return "\n".join(lines)


def _fmt(v: object) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def _authorizer(action: int, arg1: str | None, arg2: str | None, _db: str | None, _trigger: str | None) -> int:
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ:
        if arg1 == PII_TABLE and (arg2 or "") in PII_COLUMNS:
            return sqlite3.SQLITE_DENY
        if (arg1 or "").startswith("sqlite_"):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def validate_sql(sql: str) -> str:
    stmt = sql.strip().rstrip(";").strip()
    if not stmt:
        raise SQLGuardError("consulta vazia")
    if ";" in stmt:
        raise SQLGuardError("apenas uma instrução SELECT por consulta")
    if not re.match(r"^(select|with)\b", stmt, re.I):
        raise SQLGuardError("apenas consultas SELECT são permitidas")
    if FORBIDDEN_RE.search(stmt):
        raise SQLGuardError("instrução não permitida")
    return stmt


class ClaimsDB:
    def __init__(self, path: str, row_limit: int = 50, timeout_s: float = 2.0) -> None:
        self.path = path
        self.row_limit = row_limit
        self.timeout_s = timeout_s

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
        return con

    def snapshot_info(self) -> dict:
        mtime_raw = os.path.getmtime(self.path)
        cached = getattr(self, "_snapshot_cache", None)
        if cached and cached[0] == mtime_raw:
            return cached[1]
        con = self._connect()
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(self.path), tz=timezone.utc)
            latest_payment = con.execute("SELECT MAX(payment_date) FROM payments").fetchone()[0]
            latest_claim = con.execute("SELECT MAX(registration_date) FROM claims").fetchone()[0]
            counts = {
                t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("policyholders", "policies", "claims", "payments")
            }
        finally:
            con.close()
        info = {
            "snapshot_mtime": mtime.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "latest_payment_date": latest_payment,
            "latest_claim_registration": latest_claim,
            "row_counts": counts,
        }
        self._snapshot_cache = (mtime_raw, info)
        return info

    def snapshot_label(self) -> str:
        info = self.snapshot_info()
        return f"dados até {info['latest_payment_date']} (arquivo de {info['snapshot_mtime'][:10]})"

    def query_sync(self, sql: str) -> QueryResult:
        stmt = validate_sql(sql)
        con = self._connect()
        deadline = time.monotonic() + self.timeout_s
        con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 500)
        con.set_authorizer(_authorizer)
        started = time.monotonic()
        try:
            cur = con.execute(stmt)
            columns = [d[0] for d in cur.description or []]
            fetched = cur.fetchmany(self.row_limit + 1)
        except sqlite3.DatabaseError as exc:  # includes "not authorized" and "interrupted"
            msg = str(exc)
            if "not authorized" in msg or "prohibited" in msg:
                raise SQLGuardError(
                    "acesso negado: a consulta lê colunas de dados pessoais (name, cpf, phone, email, birth_date) "
                    "ou objetos não permitidos; reescreva sem essas colunas"
                ) from exc
            if "interrupted" in msg:
                raise SQLGuardError("consulta cancelada por exceder o tempo limite; simplifique a consulta") from exc
            raise SQLGuardError(f"erro de SQL: {msg}") from exc
        finally:
            con.close()
        truncated = len(fetched) > self.row_limit
        rows = [list(r) for r in fetched[: self.row_limit]]
        return QueryResult(
            sql=stmt, columns=columns, rows=rows, row_count=len(rows), truncated=truncated,
            elapsed_ms=int((time.monotonic() - started) * 1000), snapshot=self.snapshot_label(),
        )

    async def query(self, sql: str) -> QueryResult:
        return await asyncio.to_thread(self.query_sync, sql)
