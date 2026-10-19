"""全局表 users、roles（数据库设计 §3.1–3.2）。user_app_roles 依赖 apps，随 M1 迁移创建。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from aics.db.base import Base, CreatedAtMixin, SoftDeleteMixin, TimestampMixin, new_id

JSONVariant = JSON().with_variant(JSONB(), "postgresql")


class User(TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'disabled')", name="status"),
        CheckConstraint("email = lower(email)", name="email_lower"),
        Index(
            "uq_users_email_active",
            "email",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
            sqlite_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    is_super_admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    failed_logins: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # M4.1：首次登录 / 管理员重置后强制改密；改密或禁用时 +1，让旧 token 立即失效。
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class Role(CreatedAtMixin, Base):
    """种子数据，固定 3 行：super_admin / app_admin / viewer。"""

    __tablename__ = "roles"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    permissions: Mapped[list[Any]] = mapped_column(
        JSONVariant, nullable=False, server_default=text("'[]'")
    )
