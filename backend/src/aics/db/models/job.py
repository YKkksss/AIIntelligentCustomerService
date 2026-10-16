"""jobs 后台任务记录（数据库设计 §3.12）。app_id 为 NULL 表示平台级任务（如 reindex_all）。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, SmallInteger, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from aics.db.base import Base, CreatedAtMixin, new_id

JOB_TYPES = (
    "ingest",
    "replace",
    "reindex_document",
    "reindex_app",
    "reindex_all",
    "retokenize_app",
    "cleanup_document",
    "cleanup_app",
    "compact",
    "reconcile",
    "eval",
)
JOB_STATUSES = ("queued", "running", "done", "failed", "cancelled")


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


class Job(CreatedAtMixin, Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(f"type IN ({_in(JOB_TYPES)})", name="type"),
        CheckConstraint(f"status IN ({_in(JOB_STATUSES)})", name="status"),
        CheckConstraint("progress BETWEEN 0 AND 100", name="progress"),
        Index("ix_jobs_app_created", "app_id", text("created_at DESC")),
        Index("ix_jobs_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    app_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("apps.id", ondelete="CASCADE")
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    target_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'queued'"))
    progress: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )
    error: Mapped[str | None] = mapped_column(Text)
    celery_task_id: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
