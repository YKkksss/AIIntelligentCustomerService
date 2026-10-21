"""apps、user_app_roles（数据库设计 §3.3–3.4）。

配置列（rag_config 等）的结构只由 `aics.rag.config` 定义；本层只按 jsonb 存取，由 apps.service 校验与补齐默认值。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, synonym

from aics.db.base import Base, CreatedAtMixin, SoftDeleteMixin, TimestampMixin, new_id

DEFAULT_FALLBACK_TEXT = "抱歉，这个问题我暂时无法回答，建议联系人工客服。"


class App(TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "apps"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'disabled', 'deleting')", name="status"),
        CheckConstraint("answer_style IN ('concise', 'detailed', 'custom')", name="answer_style"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    # apps 自身即租户：app_id 是 id 的别名，使 AppScopedRepository 的 WHERE app_id = :app_id 同样适用
    app_id: Mapped[uuid.UUID] = synonym("id")
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    greeting: Mapped[str | None] = mapped_column(Text)
    system_prompt: Mapped[str | None] = mapped_column(Text)
    answer_style: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'concise'")
    )
    fallback_text: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text(f"'{DEFAULT_FALLBACK_TEXT}'")
    )
    language: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'zh-CN'"))
    rag_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    handoff_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    session_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )
    webhook_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )
    chunk_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # 头像只存画法（style / seed / url），不存图片；结构由 `modules/apps/avatar.py` 定义
    avatar: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )
    custom_dict: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    config_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    chunk_config_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))


class UserAppRole(CreatedAtMixin, Base):
    __tablename__ = "user_app_roles"
    __table_args__ = (
        CheckConstraint("role_id IN ('app_admin', 'viewer')", name="role_id"),
        Index("ix_user_app_roles_app_id", "app_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    app_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("apps.id", ondelete="CASCADE"), primary_key=True
    )
    role_id: Mapped[str] = mapped_column(Text, ForeignKey("roles.id"), nullable=False)
