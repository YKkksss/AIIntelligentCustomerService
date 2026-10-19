"""feedback 表（数据库设计 §3.10，迁移 `0013`，M3.6 ②）。

一条 assistant 回答最多一条反馈：`message_id` 唯一，重复提交按**覆盖写**（`ON CONFLICT DO
UPDATE`），`id` 与 `created_at` 保持不变、`updated_at` 刷新。`source` 区分写入方（开放接口写
`api`，M4.5 的后台可能写 `admin`）。

`eval_case_id` 在 M3.6 只是占位列：目标表 `eval_cases` 要等 M5.1 的迁移才存在，所以这里**不加
外键**（加入一个指向不存在表的外键会让本步的迁移直接失败），等 M5.3 建表后由那条迁移补 FK。
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from aics.db.base import Base, TimestampMixin, new_id
from aics.db.models.conversation import in_list

FEEDBACK_RATINGS = (1, -1)
FEEDBACK_SOURCES = ("api", "admin")


class Feedback(Base, TimestampMixin):
    __tablename__ = "feedback"
    __table_args__ = (
        CheckConstraint(in_list("rating", FEEDBACK_RATINGS), name="rating"),
        CheckConstraint(in_list("source", FEEDBACK_SOURCES), name="source"),
        # 每条消息一个反馈：重复提交走覆盖写，`id` 不变（接口规范 §7 的幂等风格）。
        UniqueConstraint("message_id", name="uq_feedback_message_id"),
        Index("ix_feedback_app_created", "app_id", sa_text("created_at DESC")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    app_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("apps.id", ondelete="CASCADE"), nullable=False
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    rating: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'api'"))
    handled: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa_text("false")
    )
    # M5.3（点踩转评测用例）才会写；目标表在 M5.1，故本步不加外键。
    eval_case_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
