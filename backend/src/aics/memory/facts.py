"""Key-fact reconciliation (对话上下文管理 §5.3, §7.4, §7.6; 设计讨论 0002 A1 / C3).

Mem0-style decision layer over the store primitives (add / update / retract / touch):

| op      | when                                                         |
|---------|--------------------------------------------------------------|
| ADD     | key absent                                                   |
| UPDATE  | key present and the normalised value differs, or it was retracted (revive) |
| NOOP    | same normalised value: refresh updated_at, keep the higher confidence |
| RETRACT | extractor says the user denied it (`retract=true`) and it is active |

Guards applied before deciding:
- key whitelist, value length (≤ FACT_VALUE_MAX_CHARS), confidence ≥ MIN_FACT_CONFIDENCE;
- several candidates for one key in a batch: the newest source message wins; same message →
  higher confidence (§5.3 conflict rule, covers slot vs compaction);
- **staleness**: a candidate whose source message is older than the current value's source
  message is dropped. Compaction summarises *old* turns after slot capture already stored a
  newer value; without this rule it would roll a correction back (A → B → A).
- optimistic-lock races (another writer bumped the version) are retried after re-reading.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

import structlog

from aics.core import metrics
from aics.memory.schemas import (
    FACT_VALUE_MAX_CHARS,
    MIN_FACT_CONFIDENCE,
    Fact,
    FactSource,
    FactWrite,
    is_valid_fact_key,
)
from aics.memory.store import ConversationStore, FactExistsError

log = structlog.get_logger()

SLOT_KEYS = ("order_no", "product", "phone")  # §7.6: slot keys written per turn
MAX_RACE_RETRIES = 3
_WS_RE = re.compile(r"\s+")
_PHONE_PREFIX_RE = re.compile(r"^(?:\+?86)(?=1[3-9]\d{9}$)")

Op = Literal["add", "update", "noop", "retract", "skip"]


@dataclass(frozen=True, slots=True)
class FactCandidate:
    key: str
    value: str | None
    confidence: float
    source: FactSource
    source_message_id: UUID | None = None
    retract: bool = False


@dataclass(frozen=True, slots=True)
class FactOutcome:
    key: str
    op: Op
    reason: str = ""
    value: str | None = None


# ---------------------------------------------------------------- normalisation
def normalize_value(key: str, value: str) -> str:
    """Stored form: NFKC (full-width → half-width), whitespace collapsed and trimmed.

    Identifiers are compact: phone keeps digits only (a leading +86 / 86 dropped), order_no
    drops all whitespace (users type "A2026 1014 001")."""
    v = _WS_RE.sub(" ", unicodedata.normalize("NFKC", value)).strip()
    if key == "phone":
        digits = re.sub(r"[\s\-()]", "", v)
        return _PHONE_PREFIX_RE.sub("", digits)
    if key == "order_no":
        return _WS_RE.sub("", v)
    return v


def same_value(key: str, a: str, b: str) -> bool:
    """Equality used for NOOP: normalised, whitespace-insensitive, case-insensitive."""

    def k(x: str) -> str:
        return _WS_RE.sub("", normalize_value(key, x)).casefold()

    return k(a) == k(b)


def validate(c: FactCandidate) -> str | None:
    """Reason to drop the candidate, or None."""
    if not is_valid_fact_key(c.key):
        return "bad_key"
    if not MIN_FACT_CONFIDENCE <= c.confidence <= 1:
        return "low_confidence"
    if c.retract:
        return None
    if c.value is None or not normalize_value(c.key, c.value):
        return "empty_value"
    if len(normalize_value(c.key, c.value)) > FACT_VALUE_MAX_CHARS:
        return "too_long"
    return None


def resolve_batch(
    cands: Sequence[FactCandidate], positions: dict[UUID, tuple[Any, ...]]
) -> tuple[dict[str, FactCandidate], list[FactOutcome]]:
    """One winner per key: newest source message, then highest confidence (§5.3)."""
    winners: dict[str, FactCandidate] = {}
    dropped: list[FactOutcome] = []
    for c in cands:
        reason = validate(c)
        if reason:
            dropped.append(FactOutcome(c.key, "skip", reason))
            continue
        cur = winners.get(c.key)
        if cur is None or _rank(c, positions) > _rank(cur, positions):
            if cur is not None:
                dropped.append(FactOutcome(cur.key, "skip", "superseded_in_batch"))
            winners[c.key] = c
        else:
            dropped.append(FactOutcome(c.key, "skip", "superseded_in_batch"))
    return winners, dropped


def _rank(c: FactCandidate, positions: dict[UUID, tuple[Any, ...]]) -> tuple[Any, ...]:
    pos = positions.get(c.source_message_id) if c.source_message_id else None
    # Unknown position sorts before every known one: explicit provenance wins.
    return ((1, pos) if pos is not None else (0,), c.confidence)


def _older(c: FactCandidate, cur: Fact, positions: dict[UUID, tuple[Any, ...]]) -> bool | None:
    """True if c's source is older than cur's, False if newer, None if same / unknown."""
    if c.source_message_id is None or cur.source_message_id is None:
        return None
    a, b = positions.get(c.source_message_id), positions.get(cur.source_message_id)
    if a is None or b is None or a == b:
        return None
    return a < b


# ---------------------------------------------------------------- reconcile
async def reconcile(
    store: ConversationStore,
    app_id: UUID,
    conversation_id: UUID,
    candidates: Sequence[FactCandidate],
) -> list[FactOutcome]:
    """Apply candidates inside the caller's transaction; returns one outcome per candidate
    (dropped ones included). Never raises for data problems; store errors propagate."""
    if not candidates:
        return []
    existing = {
        f.key: f for f in await store.list_facts(app_id, conversation_id, include_retracted=True)
    }
    ids = {c.source_message_id for c in candidates if c.source_message_id}
    ids |= {f.source_message_id for f in existing.values() if f.source_message_id}
    positions = await store.message_positions(app_id, conversation_id, list(ids))
    winners, outcomes = resolve_batch(candidates, positions)
    for key, c in winners.items():
        out = await _apply(store, app_id, conversation_id, c, positions)
        outcomes.append(out)
        metrics.inc("aics_fact_ops_total", op=out.op, source=c.source)
        if out.op != "skip":
            log.info("memory.fact", key=key, op=out.op, source=c.source, reason=out.reason)
    return outcomes


async def _apply(
    store: ConversationStore,
    app_id: UUID,
    cid: UUID,
    c: FactCandidate,
    positions: dict[UUID, tuple[Any, ...]],
) -> FactOutcome:
    for _ in range(MAX_RACE_RETRIES):
        cur = await store.get_fact(app_id, cid, c.key)
        if (
            cur is not None
            and c.source_message_id
            and cur.source_message_id
            and (c.source_message_id not in positions or cur.source_message_id not in positions)
        ):
            positions.update(
                await store.message_positions(
                    app_id, cid, [c.source_message_id, cur.source_message_id]
                )
            )
        older = None if cur is None else _older(c, cur, positions)
        if older:
            return FactOutcome(c.key, "skip", "stale")
        if c.retract:
            if cur is None or not cur.active:
                return FactOutcome(c.key, "skip", "nothing_to_retract")
            done = await store.retract_fact(
                app_id,
                cid,
                c.key,
                source=c.source,
                confidence=c.confidence,
                source_message_id=c.source_message_id,
                expected_version=cur.version,
            )
            if done is not None:
                return FactOutcome(c.key, "retract")
            continue
        value = normalize_value(c.key, c.value or "")
        if cur is None:
            try:
                await store.add_fact(app_id, cid, _write(c, value))
                return FactOutcome(c.key, "add", value=value)
            except FactExistsError:
                continue  # a concurrent writer added it: re-read and decide again
        if cur.active and same_value(c.key, cur.value, value):  # NOOP
            await store.touch_fact(app_id, cid, c.key, confidence=c.confidence)
            return FactOutcome(c.key, "noop", value=cur.value)
        if (
            cur.active
            and c.source_message_id is not None
            and c.source_message_id == cur.source_message_id
            and c.confidence < cur.confidence
        ):
            return FactOutcome(c.key, "skip", "lower_confidence_same_message")
        done_u = await store.update_fact(
            app_id, cid, _write(c, value), expected_version=cur.version
        )
        if done_u is not None:
            return FactOutcome(c.key, "update", "revive" if not cur.active else "", value)
    log.warning("memory.fact_race_exhausted", key=c.key)
    return FactOutcome(c.key, "skip", "race")


def _write(c: FactCandidate, value: str) -> FactWrite:
    return FactWrite(
        key=c.key,
        value=value,
        source=c.source,
        confidence=c.confidence,
        source_message_id=c.source_message_id,
    )


def slot_candidates(
    slots: dict[str, str | None] | None,
    *,
    intent_source: str,
    intent_confidence: float,
    user_message_id: UUID | None,
) -> list[FactCandidate]:
    """§7.6: only LLM-classified turns, confidence ≥ 0.5, whitelisted slot keys, non-empty."""
    if intent_source != "llm" or intent_confidence < MIN_FACT_CONFIDENCE or not slots:
        return []
    out: list[FactCandidate] = []
    for k in SLOT_KEYS:
        v = slots.get(k)
        if isinstance(v, str) and v.strip():
            out.append(
                FactCandidate(
                    key=k,
                    value=v,
                    confidence=min(1.0, float(intent_confidence)),
                    source="slot",
                    source_message_id=user_message_id,
                )
            )
    return out
