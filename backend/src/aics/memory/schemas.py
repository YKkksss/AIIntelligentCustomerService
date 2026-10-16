"""Value objects of aics.memory (对话上下文管理 §5, §7.5).

Pure data + validation; no I/O. Fact key → category / importance mapping lives here so that
both the store and the M2.5 reconciliation logic derive them the same way (never from the LLM).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from aics.db.models.fact import FACT_KEY_PATTERN

# ---------------------------------------------------------------- facts
FACT_VALUE_MAX_CHARS = 200  # §7.5 ExtractedFact.value max_length
MIN_FACT_CONFIDENCE = 0.5  # §5.3: facts below this confidence are not stored
SUMMARY_HISTORY_KEEP = 5  # §5.2: conversations.metadata.summary_history keeps the last 5

_KEY_RE = re.compile(FACT_KEY_PATTERN)
_CATEGORY = {
    "order_no": "order",
    "phone": "identity",
    "address_region": "identity",
    "product": "product",
    "issue_type": "issue",
    "solution_given": "resolution",
    "user_emotion": "emotion",
}
_IMPORTANCE = {"order_no": 3, "phone": 3, "product": 2, "issue_type": 2}

FactSource = Literal["slot", "compaction"]
FactOp = Literal["add", "update", "retract"]


def is_valid_fact_key(key: str) -> bool:
    return bool(_KEY_RE.match(key))


def fact_category(key: str) -> str:
    """order / identity / product / issue / resolution / emotion / other (§5.3)."""
    if not is_valid_fact_key(key):
        raise ValueError(f"fact key not in whitelist: {key!r}")
    return _CATEGORY.get(key, "other")


def fact_importance(key: str) -> int:
    """3 = order_no, phone; 2 = product, issue_type; 1 = everything else (§5.3)."""
    if not is_valid_fact_key(key):
        raise ValueError(f"fact key not in whitelist: {key!r}")
    return _IMPORTANCE.get(key, 1)


class FactWrite(BaseModel):
    """Input of add_fact / update_fact. category and importance are derived from `key`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    value: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=FACT_VALUE_MAX_CHARS)
    ]
    source: FactSource
    confidence: float = Field(ge=MIN_FACT_CONFIDENCE, le=1)
    source_message_id: UUID | None = None

    @field_validator("key")
    @classmethod
    def _key(cls, v: str) -> str:
        if not is_valid_fact_key(v):
            raise ValueError("fact key not in whitelist")
        return v


@dataclass(frozen=True, slots=True)
class Fact:
    id: UUID
    key: str
    value: str
    category: str
    importance: int
    confidence: float
    source: str
    version: int
    source_message_id: UUID | None
    first_seen_at: datetime
    updated_at: datetime
    retracted_at: datetime | None = None

    @property
    def active(self) -> bool:
        return self.retracted_at is None


@dataclass(frozen=True, slots=True)
class FactChange:
    """One row of conversation_fact_history."""

    id: UUID
    fact_id: UUID
    key: str
    op: str
    old_value: str | None
    new_value: str | None
    old_source_message_id: UUID | None
    new_source_message_id: UUID | None
    source: str
    confidence: float | None
    changed_at: datetime


# ---------------------------------------------------------------- summary
Item120 = Annotated[str, StringConstraints(max_length=120)]


class ConversationSummary(BaseModel):
    """Structured rolling summary stored in conversations.summary (§5.2, §7.5)."""

    model_config = ConfigDict(extra="forbid")

    user_goal: str = Field(default="", max_length=200)
    confirmed_info: list[Item120] = Field(default_factory=list, max_length=8)
    solutions_given: list[Item120] = Field(default_factory=list, max_length=8)
    open_issues: list[Item120] = Field(default_factory=list, max_length=8)


@dataclass(frozen=True, slots=True)
class SummaryState:
    summary: ConversationSummary | None
    until_msg_id: UUID | None
    version: int
    updated_at: datetime | None


# ---------------------------------------------------------------- messages
@dataclass(frozen=True, slots=True)
class StoredMessage:
    id: UUID
    role: str
    content: str
    token_count: int
    created_at: datetime
    answer_type: str | None = None
    interrupted: bool = False


@dataclass(frozen=True, slots=True)
class Turn:
    """One user message and the replies that follow it (assistant / human_agent), oldest first.

    A leading group without a user message (e.g. an agent greeting) is also a Turn.
    System messages are never part of a Turn.
    """

    messages: tuple[StoredMessage, ...] = field(default_factory=tuple)

    @property
    def token_count(self) -> int:
        return sum(m.token_count for m in self.messages)

    @property
    def first_id(self) -> UUID:
        return self.messages[0].id

    @property
    def last_id(self) -> UUID:
        return self.messages[-1].id


@dataclass(frozen=True, slots=True)
class Hit:
    """In-conversation keyword recall hit (§8)."""

    message_id: UUID
    role: str
    content: str
    created_at: datetime
    score: float


def group_turns(messages: list[StoredMessage]) -> list[Turn]:
    """Group oldest→newest non-system messages into turns; returns newest turn first."""
    turns: list[list[StoredMessage]] = []
    for m in messages:
        if m.role == "system":
            continue
        if m.role == "user" or not turns:
            turns.append([m])
        else:
            turns[-1].append(m)
    return [Turn(tuple(t)) for t in reversed(turns)]
