"""ConversationStore: the data-access port of aics.memory (对话上下文管理 §0).

Two implementations share one contract (tests/memory_store_contract.py):
- `aics.memory.repository.PgConversationStore` (PostgreSQL, production)
- `InMemoryConversationStore` below (unit tests, eval dry-runs)

M2.1 scope: storage primitives. The fact methods are deliberately low level
(add / update / retract / touch, each with optimistic `expected_version`); deciding *which*
op to apply (normalisation, ADD / UPDATE / NOOP / RETRACT reconciliation, slot vs compaction
conflicts) is `memory.facts` in M2.5. Every fact mutation that changes the value or the
retraction state appends exactly one history row in the same transaction; there is no API
to update or delete history rows.

The store never commits: the caller (memory.service) owns the transaction.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from aics.core.errors import ConflictError, ConversationNotFoundError, ValidationAppError
from aics.core.textsearch import tokenize_for_index
from aics.core.tokens import count_tokens
from aics.db.base import new_id
from aics.memory.schemas import (
    MIN_FACT_CONFIDENCE,
    SUMMARY_HISTORY_KEEP,
    ConversationSummary,
    Fact,
    FactChange,
    FactSource,
    FactWrite,
    Hit,
    StoredMessage,
    SummaryState,
    Turn,
    fact_category,
    fact_importance,
    group_turns,
)

# Upper bound of messages loaded by turns_after; compaction keeps the unsummarised tail far
# below this, so hitting it means compaction is failing (load_context then flags truncation).
MAX_TURN_MESSAGES = 1000
MAX_RECALL_K = 50


class FactExistsError(ConflictError):
    code, message = "FACT_EXISTS", "该会话已存在同键事实"


class FactNotFoundError(ConflictError):
    code, message = "FACT_NOT_FOUND", "该会话不存在此键的事实"


def check_k(k: int) -> None:
    if not 1 <= k <= MAX_RECALL_K:
        raise ValidationAppError(message=f"k must be within 1..{MAX_RECALL_K}")


def check_confidence(confidence: float) -> None:
    if not 0 <= confidence <= 1:
        raise ValidationAppError(message="confidence must be within 0..1")


class ConversationStore(Protocol):
    # ---- summary (conversations.summary*) ----
    async def get_summary_state(self, app_id: UUID, conversation_id: UUID) -> SummaryState: ...

    async def save_summary(
        self,
        app_id: UUID,
        conversation_id: UUID,
        summary: ConversationSummary,
        until_msg_id: UUID,
        expected_version: int,
    ) -> bool:
        """Optimistic write: True and version+1 if the stored version equals expected_version."""
        ...

    # ---- messages (read only) ----
    async def turns_after(
        self, app_id: UUID, conversation_id: UUID, after_msg_id: UUID | None
    ) -> list[Turn]:
        """Turns strictly after `after_msg_id` (None = from the start), newest turn first."""
        ...

    async def search_messages(
        self,
        app_id: UUID,
        conversation_id: UUID,
        tsquery: str,
        exclude_ids: Sequence[UUID],
        k: int,
    ) -> list[Hit]:
        """Keyword recall inside one conversation; `tsquery` comes from core.textsearch."""
        ...

    async def is_app_deleting(self, app_id: UUID) -> bool:
        """True when the app is being deleted (compaction then stops, §7.2)."""
        ...

    async def message_positions(
        self, app_id: UUID, conversation_id: UUID, message_ids: Sequence[UUID]
    ) -> dict[UUID, tuple[Any, ...]]:
        """Comparable positions (older < newer) of messages of this conversation; unknown ids
        are omitted. Used to decide which of two fact writes is newer (M2.5)."""
        ...

    # ---- facts ----
    async def list_facts(
        self, app_id: UUID, conversation_id: UUID, *, include_retracted: bool = False
    ) -> list[Fact]:
        """Ordered by importance desc, then updated_at desc (injection order, 设计讨论 0002 C2)."""
        ...

    async def get_fact(self, app_id: UUID, conversation_id: UUID, key: str) -> Fact | None: ...

    async def add_fact(self, app_id: UUID, conversation_id: UUID, data: FactWrite) -> Fact:
        """ADD: insert version 1 + history `add`. FactExistsError if the key exists."""
        ...

    async def update_fact(
        self, app_id: UUID, conversation_id: UUID, data: FactWrite, *, expected_version: int
    ) -> Fact | None:
        """UPDATE: new value (also revives a retracted fact) + history `update`.

        Returns None if the stored version is not `expected_version` (lost race).
        """
        ...

    async def retract_fact(
        self,
        app_id: UUID,
        conversation_id: UUID,
        key: str,
        *,
        source: FactSource,
        confidence: float,
        source_message_id: UUID | None,
        expected_version: int,
    ) -> Fact | None:
        """RETRACT: set retracted_at, version+1, history `retract`. None on version race."""
        ...

    async def touch_fact(
        self, app_id: UUID, conversation_id: UUID, key: str, *, confidence: float
    ) -> Fact | None:
        """NOOP: refresh updated_at and keep the higher confidence; no history row."""
        ...

    async def list_fact_history(
        self, app_id: UUID, conversation_id: UUID, *, key: str | None = None
    ) -> list[FactChange]:
        """History oldest first."""
        ...


def check_update(current: Fact, data: FactWrite) -> None:
    if current.active and current.value == data.value:
        raise ValidationAppError(message="value unchanged: use touch_fact (NOOP)")


def fact_sort_key(f: Fact) -> tuple[int, float]:
    return (-f.importance, -f.updated_at.timestamp())


# ====================================================================== in-memory
_TERM_RE = re.compile(r"'([^']+)'")


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class _Conv:
    app_id: UUID
    summary: dict | None = None
    until_msg_id: UUID | None = None
    version: int = 0
    updated_at: datetime | None = None
    summary_history: list[dict] | None = None


@dataclass
class _Msg:
    message: StoredMessage
    search_text: str


class InMemoryConversationStore:
    """Dict-backed ConversationStore with the same observable behaviour as the PG store.

    `create_conversation` / `add_message` exist only here, to seed data in tests; in production
    messages are written by chat.service (MessageRepository.append).
    """

    def __init__(self) -> None:
        self._convs: dict[UUID, _Conv] = {}
        self._msgs: dict[UUID, list[_Msg]] = {}
        self._facts: dict[tuple[UUID, str], Fact] = {}
        self._history: dict[UUID, list[FactChange]] = {}
        self.deleting_apps: set[UUID] = set()

    # ---- seeding helpers ----
    def create_conversation(self, app_id: UUID) -> UUID:
        cid = new_id()
        self._convs[cid] = _Conv(app_id=app_id)
        self._msgs[cid] = []
        self._history[cid] = []
        return cid

    def add_message(
        self,
        app_id: UUID,
        conversation_id: UUID,
        role: str,
        content: str,
        *,
        custom_dict: Sequence[str] = (),
        answer_type: str | None = None,
        interrupted: bool = False,
    ) -> StoredMessage:
        self._conv(app_id, conversation_id)
        msg = StoredMessage(
            id=new_id(),
            role=role,
            content=content,
            token_count=count_tokens(content),
            created_at=_now(),
            answer_type=answer_type,
            interrupted=interrupted,
        )
        search = "" if role == "system" else tokenize_for_index(app_id, content, custom_dict)
        self._msgs[conversation_id].append(_Msg(msg, search))
        return msg

    def summary_history(self, app_id: UUID, conversation_id: UUID) -> list[dict]:
        return list(self._conv(app_id, conversation_id).summary_history or [])

    def _conv(self, app_id: UUID, conversation_id: UUID) -> _Conv:
        conv = self._convs.get(conversation_id)
        if conv is None or conv.app_id != app_id:
            raise ConversationNotFoundError(details={"id": str(conversation_id)})
        return conv

    # ---- summary ----
    async def get_summary_state(self, app_id: UUID, conversation_id: UUID) -> SummaryState:
        c = self._conv(app_id, conversation_id)
        summary = None if c.summary is None else ConversationSummary.model_validate(c.summary)
        return SummaryState(summary, c.until_msg_id, c.version, c.updated_at)

    async def save_summary(
        self,
        app_id: UUID,
        conversation_id: UUID,
        summary: ConversationSummary,
        until_msg_id: UUID,
        expected_version: int,
    ) -> bool:
        c = self._conv(app_id, conversation_id)
        if not any(m.message.id == until_msg_id for m in self._msgs[conversation_id]):
            raise ValidationAppError(message="until_msg_id is not a message of this conversation")
        if c.version != expected_version:
            return False
        now = _now()
        c.summary = summary.model_dump()
        c.until_msg_id = until_msg_id
        c.version += 1
        c.updated_at = now
        entry = {
            "version": c.version,
            "summary": c.summary,
            "until_msg_id": str(until_msg_id),
            "updated_at": now.isoformat(),
        }
        c.summary_history = [*(c.summary_history or []), entry][-SUMMARY_HISTORY_KEEP:]
        return True

    # ---- messages ----
    async def turns_after(
        self, app_id: UUID, conversation_id: UUID, after_msg_id: UUID | None
    ) -> list[Turn]:
        self._conv(app_id, conversation_id)
        msgs = [m.message for m in self._msgs[conversation_id]]
        if after_msg_id is not None:
            idx = next((i for i, m in enumerate(msgs) if m.id == after_msg_id), None)
            if idx is None:
                raise ValidationAppError(
                    message="after_msg_id is not a message of this conversation"
                )
            msgs = msgs[idx + 1 :]
        msgs = [m for m in msgs if m.role != "system"][-MAX_TURN_MESSAGES:]
        return group_turns(msgs)

    async def search_messages(
        self,
        app_id: UUID,
        conversation_id: UUID,
        tsquery: str,
        exclude_ids: Sequence[UUID],
        k: int,
    ) -> list[Hit]:
        check_k(k)
        self._conv(app_id, conversation_id)
        terms = set(_TERM_RE.findall(tsquery or ""))
        if not terms:
            return []
        excluded = set(exclude_ids)
        hits: list[Hit] = []
        for m in self._msgs[conversation_id]:
            if m.message.role == "system" or m.message.id in excluded:
                continue
            tokens = m.search_text.lower().split()
            score = float(sum(tokens.count(t) for t in terms))
            if score > 0:
                hits.append(
                    Hit(
                        m.message.id, m.message.role, m.message.content, m.message.created_at, score
                    )
                )
        hits.sort(key=lambda h: (-h.score, -h.created_at.timestamp()))
        return hits[:k]

    async def is_app_deleting(self, app_id: UUID) -> bool:
        return app_id in self.deleting_apps

    async def message_positions(
        self, app_id: UUID, conversation_id: UUID, message_ids: Sequence[UUID]
    ) -> dict[UUID, tuple[Any, ...]]:
        self._conv(app_id, conversation_id)
        wanted = set(message_ids)
        return {
            m.message.id: (i,)
            for i, m in enumerate(self._msgs[conversation_id])
            if m.message.id in wanted
        }

    # ---- facts ----
    async def list_facts(
        self, app_id: UUID, conversation_id: UUID, *, include_retracted: bool = False
    ) -> list[Fact]:
        self._conv(app_id, conversation_id)
        facts = [
            f
            for (cid, _), f in self._facts.items()
            if cid == conversation_id and (include_retracted or f.active)
        ]
        return sorted(facts, key=fact_sort_key)

    async def get_fact(self, app_id: UUID, conversation_id: UUID, key: str) -> Fact | None:
        self._conv(app_id, conversation_id)
        return self._facts.get((conversation_id, key))

    def _log(self, conversation_id: UUID, change: FactChange) -> None:
        self._history[conversation_id].append(change)

    async def add_fact(self, app_id: UUID, conversation_id: UUID, data: FactWrite) -> Fact:
        self._conv(app_id, conversation_id)
        if (conversation_id, data.key) in self._facts:
            raise FactExistsError(details={"key": data.key})
        now = _now()
        fact = Fact(
            id=new_id(),
            key=data.key,
            value=data.value,
            category=fact_category(data.key),
            importance=fact_importance(data.key),
            confidence=data.confidence,
            source=data.source,
            version=1,
            source_message_id=data.source_message_id,
            first_seen_at=now,
            updated_at=now,
        )
        self._facts[(conversation_id, data.key)] = fact
        self._log(
            conversation_id,
            FactChange(
                new_id(),
                fact.id,
                data.key,
                "add",
                None,
                data.value,
                None,
                data.source_message_id,
                data.source,
                data.confidence,
                now,
            ),
        )
        return fact

    async def update_fact(
        self, app_id: UUID, conversation_id: UUID, data: FactWrite, *, expected_version: int
    ) -> Fact | None:
        current = await self.get_fact(app_id, conversation_id, data.key)
        if current is None:
            raise FactNotFoundError(details={"key": data.key})
        check_update(current, data)
        if current.version != expected_version:
            return None
        now = _now()
        new = replace(
            current,
            value=data.value,
            confidence=data.confidence,
            source=data.source,
            source_message_id=data.source_message_id,
            version=current.version + 1,
            updated_at=now,
            retracted_at=None,
        )
        self._facts[(conversation_id, data.key)] = new
        self._log(
            conversation_id,
            FactChange(
                new_id(),
                current.id,
                data.key,
                "update",
                current.value,
                data.value,
                current.source_message_id,
                data.source_message_id,
                data.source,
                data.confidence,
                now,
            ),
        )
        return new

    async def retract_fact(
        self,
        app_id: UUID,
        conversation_id: UUID,
        key: str,
        *,
        source: FactSource,
        confidence: float,
        source_message_id: UUID | None,
        expected_version: int,
    ) -> Fact | None:
        check_confidence(confidence)
        current = await self.get_fact(app_id, conversation_id, key)
        if current is None:
            raise FactNotFoundError(details={"key": key})
        if current.version != expected_version or not current.active:
            return None
        now = _now()
        new = replace(current, version=current.version + 1, updated_at=now, retracted_at=now)
        self._facts[(conversation_id, key)] = new
        self._log(
            conversation_id,
            FactChange(
                new_id(),
                current.id,
                key,
                "retract",
                current.value,
                None,
                current.source_message_id,
                source_message_id,
                source,
                confidence,
                now,
            ),
        )
        return new

    async def touch_fact(
        self, app_id: UUID, conversation_id: UUID, key: str, *, confidence: float
    ) -> Fact | None:
        check_confidence(confidence)
        current = await self.get_fact(app_id, conversation_id, key)
        if current is None:
            return None
        new = replace(current, confidence=max(current.confidence, confidence), updated_at=_now())
        self._facts[(conversation_id, key)] = new
        return new

    async def list_fact_history(
        self, app_id: UUID, conversation_id: UUID, *, key: str | None = None
    ) -> list[FactChange]:
        self._conv(app_id, conversation_id)
        return [c for c in self._history[conversation_id] if key is None or c.key == key]


__all__ = [
    "MIN_FACT_CONFIDENCE",
    "ConversationStore",
    "FactExistsError",
    "FactNotFoundError",
    "InMemoryConversationStore",
]
