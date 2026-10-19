"""conversation_events, webhook_deliveries —— 会话事件 outbox 与出站投递（ADR-0016）。

See docs/03-database/01-数据库设计.md §3.15–3.16, docs/02-architecture/adr/0016.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
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
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from aics.db.base import Base, new_id
from aics.db.models.conversation import in_list

# ---------------------------------------------------------------- enumerations
# L1：事件名与投递状态（数据库 CHECK、webhooks.payloads 与事件流共用一份定义）。
EVENT_TYPES = (
    "handoff.requested",
    "handoff.accepted",
    "handoff.released",
    "handoff.queue_timeout",
    "handoff.offline",
    "message.created",
    "conversation.idle_notice",
    "conversation.closed",
)
DELIVERY_STATUSES = ("pending", "delivered", "failed")


class ConversationEvent(Base):
    """一条会话事件：与状态变更同事务写入，`seq` 由 `conversations.event_seq` 递增分配。

    `seq` 是事件流的游标（SSE `id:` 行与 `Last-Event-ID` 补发都以它为准），事件流订阅方
    拿到的是「同会话递增、无空洞」的整数；`payload` 由 `webhooks.payloads` 生成，
    所以事件行只存 `data`、公共字段在投递/推送时现拼。
    """

    __tablename__ = "conversation_events"
    __table_args__ = (
        CheckConstraint(in_list("type", EVENT_TYPES), name="type"),
        UniqueConstraint("conversation_id", "seq"),
        Index("ix_conversation_events_app_created", "app_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    app_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("apps.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WebhookDelivery(Base):
    """一条待投递（或已投递）的 Webhook：一个事件最多一条，`id` 与 `event_id` 同值。

    `id` 既是投递 ID 也是 payload 的 `id`（接入方按它去重），与设计 §3.16 一致。
    `payload` 存的是最终发出的 JSON，重试时原样重发（签名用新时间戳重算）。
    """

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        CheckConstraint(in_list("status", DELIVERY_STATUSES), name="status"),
        CheckConstraint("attempts >= 0", name="attempts"),
        Index("ix_webhook_deliveries_app_created", "app_id", sa_text("created_at DESC")),
        Index("ix_webhook_deliveries_status_next_attempt", "status", "next_attempt_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversation_events.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    app_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    event: Mapped[str] = mapped_column(Text, nullable=False)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'pending'"))
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=sa_text("0"))
    last_status_code: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
