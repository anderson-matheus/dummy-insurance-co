import pytest

from app.knowledge.claims_db import ClaimsDB, SQLGuardError, validate_sql
from tests.conftest import CLAIMS_DB


@pytest.fixture(scope="module")
def db():
    return ClaimsDB(str(CLAIMS_DB), row_limit=10)


def test_join_uses_paid_amount(db):
    res = db.query_sync(
        "SELECT c.claim_number, c.claim_amount, p.paid_amount FROM claims c JOIN payments p ON p.claim_id = c.claim_id WHERE c.claim_number = 'SIN-2025-004512'"
    )
    assert res.rows == [["SIN-2025-004512", 120000.0, 100000.0]]
    assert "SIN-2025-004512 | 120000.00 | 100000.00" in res.as_text()
    assert res.snapshot.startswith("dados até 2026-02-11")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT name FROM policyholders LIMIT 1",
        "SELECT * FROM policyholders LIMIT 1",
        "SELECT cpf FROM policyholders WHERE policyholder_id = 1",
        "SELECT p.paid_amount FROM payments p JOIN claims c ON c.claim_id = p.claim_id JOIN policies po ON po.policy_id = c.policy_id JOIN policyholders ph ON ph.policyholder_id = po.policyholder_id WHERE ph.phone LIKE '%0001'",
    ],
)
def test_pii_columns_are_denied(db, sql):
    with pytest.raises(SQLGuardError) as exc:
        db.query_sync(sql)
    assert "dados pessoais" in str(exc.value)


def test_non_pii_policyholder_columns_allowed(db):
    res = db.query_sync("SELECT state, COUNT(*) AS n FROM policyholders GROUP BY state ORDER BY n DESC LIMIT 3")
    assert res.columns == ["state", "n"] and len(res.rows) == 3


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE claims SET status = 'Pago'",
        "DELETE FROM payments",
        "SELECT 1; DROP TABLE claims",
        "PRAGMA table_info(claims)",
        "ATTACH DATABASE '/tmp/x.db' AS x",
        "SELECT sql FROM sqlite_master",
        "",
    ],
)
def test_non_select_statements_rejected(sql):
    with pytest.raises(SQLGuardError):
        validate_sql(sql)


def test_row_cap_and_truncation(db):
    res = db.query_sync("SELECT claim_number FROM claims")
    assert res.row_count == 10 and res.truncated
    assert "truncado" in res.as_text()


def test_sql_error_is_reported_safely(db):
    with pytest.raises(SQLGuardError) as exc:
        db.query_sync("SELECT nope FROM claims")
    assert "erro de SQL" in str(exc.value)


def test_snapshot_info(db):
    info = db.snapshot_info()
    assert info["row_counts"]["claims"] == 10000
    assert info["latest_payment_date"] == "2026-02-11"
