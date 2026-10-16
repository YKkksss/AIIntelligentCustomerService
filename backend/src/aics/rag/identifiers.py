"""Identifier consistency guardrail (RAG 流程设计 §4 postprocess; 设计讨论 0002 A4).

Order-number-like (`[A-Za-z0-9-]{8,32}` with ≥ 6 digits) and phone-like (`1[3-9]\\d{9}`)
strings in an answer must come from somewhere legitimate: the current facts (`order_no` /
`phone`, active only — retracted facts never reach the state), the current user message, or
the sources sent to generation. Anything else is flagged `identifier_mismatch` (detect and
alert only; the streamed text is never rewritten).
Dates (2026-10-14, 2026/10/14, 20261014) are not identifiers.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9-])[A-Za-z0-9-]{8,32}(?![A-Za-z0-9-])")
_PHONE_RE = re.compile(r"1[3-9]\d{9}")
_DATE_RE = re.compile(r"(?:19|20)\d{2}-?(?:0[1-9]|1[0-2])-?(?:0[1-9]|[12]\d|3[01])")
FACT_KEYS = ("order_no", "phone")


@dataclass(frozen=True)
class IdentifierCheck:
    checked: int = 0
    mismatched: list[tuple[str, str]] = field(default_factory=list)  # (kind, value)

    @property
    def mismatch(self) -> bool:
        return bool(self.mismatched)


def _norm(s: str) -> str:
    return re.sub(r"[\s\-]", "", unicodedata.normalize("NFKC", s)).casefold()


def find_identifiers(text: str) -> list[tuple[str, str]]:
    """(kind, value) pairs in order of appearance; kind is "phone" or "order"."""
    out: list[tuple[str, str]] = []
    for m in _TOKEN_RE.finditer(unicodedata.normalize("NFKC", text)):
        tok = m.group(0).strip("-")
        if sum(ch.isdigit() for ch in tok) < 6 or _DATE_RE.fullmatch(tok):
            continue
        out.append(("phone" if _PHONE_RE.fullmatch(tok) else "order", tok))
    return out


def check_identifiers(
    answer: str,
    *,
    facts: Mapping[str, str],
    question: str,
    sources: Iterable[str],
) -> IdentifierCheck:
    found = find_identifiers(answer)
    if not found:
        return IdentifierCheck()
    allowed = {_norm(v) for k, v in facts.items() if k in FACT_KEYS and v}
    haystack = _norm(question) + "\n" + "\n".join(_norm(s) for s in sources)
    bad = [
        (kind, tok)
        for kind, tok in found
        if _norm(tok) not in allowed and _norm(tok) not in haystack
    ]
    return IdentifierCheck(len(found), bad)
