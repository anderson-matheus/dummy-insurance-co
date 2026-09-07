"""PII protection helpers: pattern masking plus a policyholder-name blocklist.

Used at ingestion (committee minutes contain names/CPFs/phones) and on every
answer before it reaches the user or the history database.
"""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from typing import Iterable

MASK = "[dado pessoal omitido]"

CPF_RE = re.compile(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b|\b\d{11}\b")
PHONE_RE = re.compile(r"\(?\b\d{2}\)?\s?9?\d{4}-\d{4}\b")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# capitalised spans of 2-5 words (with optional "da/de/do/dos/das" connectors)
NAME_SPAN_RE = re.compile(
    r"\b[A-ZÁÉÍÓÚÂÊÔÃÕÇ][a-záéíóúâêôãõç]+(?:\s+(?:d[aeo]s?\s+)?[A-ZÁÉÍÓÚÂÊÔÃÕÇ][a-záéíóúâêôãõç]+){1,4}\b"
)
CONNECTORS = {"da", "de", "do", "das", "dos"}


def normalize(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(ch)
    )


def load_policyholder_names(claims_db_path: str) -> list[str]:
    con = sqlite3.connect(f"file:{claims_db_path}?mode=ro", uri=True)
    try:
        return [r[0] for r in con.execute("SELECT name FROM policyholders")]
    finally:
        con.close()


class PIIScrubber:
    def __init__(self, names: Iterable[str] = ()) -> None:
        self.full_names: set[str] = set()
        self.pairs: set[tuple[str, str]] = set()
        for name in names:
            words = [w for w in normalize(name).split() if w not in CONNECTORS]
            if len(words) < 2:
                continue
            self.full_names.add(" ".join(words))
            self.pairs.add((words[0], words[-1]))
            self.pairs.add((words[0], words[1]))

    def _is_name(self, span: str) -> bool:
        words = [w for w in normalize(span).split() if w not in CONNECTORS]
        if len(words) < 2:
            return False
        if " ".join(words) in self.full_names:
            return True
        return (words[0], words[-1]) in self.pairs or (words[0], words[1]) in self.pairs

    def scrub(self, text: str) -> tuple[str, int]:
        hits = 0

        def repl(_m: re.Match) -> str:
            nonlocal hits
            hits += 1
            return MASK

        text = CPF_RE.sub(repl, text)
        text = EMAIL_RE.sub(repl, text)
        text = PHONE_RE.sub(repl, text)
        if self.full_names:

            def name_repl(m: re.Match) -> str:
                nonlocal hits
                if self._is_name(m.group(0)):
                    hits += 1
                    return MASK
                return m.group(0)

            text = NAME_SPAN_RE.sub(name_repl, text)
        return text, hits
