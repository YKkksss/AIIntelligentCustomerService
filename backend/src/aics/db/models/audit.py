"""audit_logs（数据库设计 §3.13，迁移 `0014`，M4.1 ②）。

只追加，没有更新 / 删除的接口；`id` 用 IDENTITY 自增（比 UUID 省空间，且天然按写入顺序）。
`app_id` / `actor_id` **不建外键**：审计要能解释已删除的应用与用户，外键的 CASCADE 或 SET NULL
会把这段历史抹掉。`diff` 里的敏感字段由 `core.audit.redact()` 在写入前脱敏。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Identity, Index, Text, func
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from aics.db.base import Base
from aics.db.models.conversation import in_list

AUDIT_ACTOR_TYPES = ("user", "api_key", "system")


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        CheckConstraint(in_list("actor_type", AUDIT_ACTOR_TYPES), name="actor_type"),
        Index("ix_audit_logs_app_created", "app_id", sa_text("created_at DESC")),
        Index("ix_audit_logs_actor_created", "actor_id", sa_text("created_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    actor_type: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    app_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(Text, nullable=False)
    target_type: Mapped[str | None] = mapped_column(Text)
    target_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    diff: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
