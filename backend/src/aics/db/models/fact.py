"""conversation_facts and conversation_fact_history (数据库设计 §3.9.1–3.9.2; 设计讨论 0002).

Facts are versioned: updates and retractions append to the history table and never lose the
old value. Writing (reconciliation ADD / UPDATE / NOOP / RETRACT) is done by aics.memory (M2.5).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import REAL, UUID
from sqlalchemy.orm import Mapped, mapped_column

from aics.db.base import Base, TimestampMixin, new_id
from aics.db.models.conversation import in_list

FACT_CATEGORIES = ("order", "identity", "product", "issue", "resolution", "emotion", "other")
FACT_SOURCES = ("slot", "compaction")
FACT_OPS = ("add", "update", "retract")
# Whitelisted fact keys (对话上下文管理 §5.3); `other:<name>` allows a short snake_case suffix.
FACT_KEY_PATTERN = (
    r"^(order_no|product|issue_type|phone|address_region|solution_given|user_emotion"
    r"|other:[a-z_]{1,32})$"
)


class ConversationFact(TimestampMixin, Base):
    """Current value of one key fact per (conversation, key); never deleted except by cascade."""

    __tablename__ = "conversation_facts"
    __table_args__ = (
        UniqueConstraint("conversation_id", "key"),
        CheckConstraint(f"key ~ '{FACT_KEY_PATTERN}'", name="key"),
        CheckConstraint("length(value) > 0", name="value"),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="confidence"),
        CheckConstraint(in_list("category", FACT_CATEGORIES), name="category"),
        CheckConstraint("importance BETWEEN 1 AND 3", name="importance"),
        CheckConstraint(in_list("source", FACT_SOURCES), name="source"),
        CheckConstraint("version >= 1", name="version"),
        Index("ix_conversation_facts_app_conversation", "app_id", "conversation_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    app_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    confidence: Mapped[float] = mapped_column(REAL(), nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    importance: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("1"))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    retracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConversationFactHistory(Base):
    """Append-only version history of conversation facts (UPDATE is rejected by a DB trigger)."""

    __tablename__ = "conversation_fact_history"
    __table_args__ = (
        CheckConstraint(in_list("op", FACT_OPS), name="op"),
        # Shape of each op: add has no old value, retract has no new value, update has both.
        CheckConstraint(
            "(op = 'add' AND old_value IS NULL AND new_value IS NOT NULL)"
            " OR (op = 'update' AND old_value IS NOT NULL AND new_value IS NOT NULL)"
            " OR (op = 'retract' AND new_value IS NULL)",
            name="op_values",
        ),
        CheckConstraint(in_list("source", FACT_SOURCES), name="source"),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="confidence"),
        Index(
            "ix_conversation_fact_history_app_conversation_changed",
            "app_id",
            "conversation_id",
            "changed_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    fact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversation_facts.id", ondelete="CASCADE"), nullable=False
    )
    app_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    op: Mapped[str] = mapped_column(Text, nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    old_source_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    new_source_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    source: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float | None] = mapped_column(REAL())
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )


__all__ = ["ConversationFact", "ConversationFactHistory"]
