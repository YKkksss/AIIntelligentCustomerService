"""conversations, messages, system_settings.

See docs/03-database/01-数据库设计.md §3.8–3.9 and §3.14, and
docs/02-architecture/07-对话上下文管理.md §5.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, REAL, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from aics.db.base import Base, new_id

# ---------------------------------------------------------------- enumerations
# Kept here (L1) so that every layer validates against one definition.
CONVERSATION_CHANNELS = ("api", "openai", "debug", "eval")
CONVERSATION_STATUSES = ("active", "closed")
CLOSE_REASONS = ("idle", "handoff_idle", "app_deleted", "eval_done")
HANDOFF_STATUSES = ("none", "pending", "accepted", "released")
HANDOFF_END_REASONS = ("agent", "admin", "queue_timeout", "offline", "accepted_idle")
CALLBACK_REASONS = ("queue_timeout", "offline")

MESSAGE_ROLES = ("user", "assistant", "human_agent", "system")
MESSAGE_KINDS = (
    "chat",
    "idle_notice",
    "idle_closed",
    "handoff_wait",
    "handoff_accepted",
    "handoff_released",
    "handoff_queue_timeout",
    "handoff_offline",
    "handoff_idle_closed",
)
ANSWER_TYPES = ("answer", "refused", "handoff", "error")
INTENTS = (
    "kb_qa",
    "chitchat",
    "human",
    "out_of_scope",
    "action",
    "clarify",
    "recall",
    "blocked",
)
INTENT_SOURCES = ("rule", "vector", "llm", "fallback")


def in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint(in_list("channel", CONVERSATION_CHANNELS), name="channel"),
        CheckConstraint(in_list("status", CONVERSATION_STATUSES), name="status"),
        CheckConstraint(in_list("close_reason", CLOSE_REASONS), name="close_reason"),
        CheckConstraint(in_list("handoff_status", HANDOFF_STATUSES), name="handoff_status"),
        CheckConstraint(
            in_list("handoff_end_reason", HANDOFF_END_REASONS), name="handoff_end_reason"
        ),
        CheckConstraint(in_list("callback_reason", CALLBACK_REASONS), name="callback_reason"),
        CheckConstraint("message_count >= 0", name="message_count"),
        CheckConstraint("refusal_streak >= 0", name="refusal_streak"),
        CheckConstraint("summary_version >= 0", name="summary_version"),
        # A closed conversation always records when; an active one never does.
        CheckConstraint("(status = 'closed') = (closed_at IS NOT NULL)", name="closed_at"),
        Index("ix_conversations_app_last_message", "app_id", sa_text("last_message_at DESC")),
        Index("ix_conversations_app_external_user", "app_id", "external_user_id"),
        Index(
            "ix_conversations_active_activity",
            "last_activity_at",
            postgresql_where=sa_text("status = 'active'"),
        ),
        Index(
            "ix_conversations_app_handoff",
            "app_id",
            "handoff_status",
            postgresql_where=sa_text("handoff_status IN ('pending', 'accepted')"),
        ),
        Index(
            "ix_conversations_app_callback",
            "app_id",
            sa_text("callback_requested_at DESC"),
            postgresql_where=sa_text("needs_callback"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    app_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("apps.id"), nullable=False
    )
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'active'"))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    close_reason: Mapped[str | None] = mapped_column(Text)
    previous_conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL")
    )
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    idle_notice_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    external_user_id: Mapped[str | None] = mapped_column(Text)
    api_key_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    title: Mapped[str | None] = mapped_column(Text)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    refusal_streak: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sa_text("0")
    )
    handoff_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sa_text("'none'")
    )
    handoff_reason: Mapped[str | None] = mapped_column(Text)
    handoff_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    handoff_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    handoff_released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    handoff_end_reason: Mapped[str | None] = mapped_column(Text)
    needs_callback: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("false")
    )
    callback_reason: Mapped[str | None] = mapped_column(Text)
    callback_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    callback_cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    callback_cleared_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id")
    )
    callback_note: Mapped[str | None] = mapped_column(Text)
    handoff_agent_id: Mapped[str | None] = mapped_column(Text)
    handoff_agent_name: Mapped[str | None] = mapped_column(Text)
    event_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=sa_text("0"))
    # Rolling structured summary (对话上下文管理 §5.2 / §7); written only by aics.memory.
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    summary_until_msg_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    summary_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sa_text("0")
    )
    summary_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=sa_text("'{}'")
    )
    last_message_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint(in_list("role", MESSAGE_ROLES), name="role"),
        CheckConstraint(in_list("kind", MESSAGE_KINDS), name="kind"),
        # Only system messages carry a non-chat kind.
        CheckConstraint("role = 'system' OR kind = 'chat'", name="kind_role"),
        CheckConstraint(in_list("answer_type", ANSWER_TYPES), name="answer_type"),
        CheckConstraint(in_list("intent", INTENTS), name="intent"),
        CheckConstraint(in_list("intent_source", INTENT_SOURCES), name="intent_source"),
        CheckConstraint("intent_confidence BETWEEN 0 AND 1", name="intent_confidence"),
        CheckConstraint("token_count >= 0", name="token_count"),
        # Answer metadata belongs to assistant messages only (数据库设计 §3.9).
        CheckConstraint(
            "role = 'assistant' OR (answer_type IS NULL AND intent IS NULL"
            " AND intent_confidence IS NULL AND intent_source IS NULL"
            " AND citations IS NULL AND NOT handoff)",
            name="assistant_only_fields",
        ),
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
        Index("ix_messages_app_created", "app_id", sa_text("created_at DESC")),
        Index("ix_messages_app_answer_type", "app_id", "answer_type"),
        Index("ix_messages_app_intent_created", "app_id", "intent", sa_text("created_at DESC")),
        Index("ix_messages_tsv", "tsv", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    app_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'chat'"))
    agent_id: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    search_text: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("''"))
    tsv: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple'::regconfig, search_text)", persisted=True)
    )
    answer_type: Mapped[str | None] = mapped_column(Text)
    intent: Mapped[str | None] = mapped_column(Text)
    intent_confidence: Mapped[float | None] = mapped_column(REAL())
    intent_source: Mapped[str | None] = mapped_column(Text)
    slots: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    rewritten_query: Mapped[str | None] = mapped_column(Text)
    citations: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB(none_as_null=True))
    retrieval_trace: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    decision_reason: Mapped[str | None] = mapped_column(Text)
    handoff: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sa_text("false"))
    degraded: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=sa_text("'{}'")
    )
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    first_token_ms: Mapped[int | None] = mapped_column(Integer)
    llm_model: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    config_version: Mapped[int | None] = mapped_column(Integer)
    interrupted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa_text("false")
    )
    # clock_timestamp() (not now()) so that several messages written in one transaction
    # still get strictly increasing timestamps; ordering is (created_at, id).
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
    )


class SystemSetting(Base):
    """Platform-wide key/value settings (数据库设计 §3.14); not app-scoped."""

    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


__all__ = ["Conversation", "Message", "SystemSetting"]
