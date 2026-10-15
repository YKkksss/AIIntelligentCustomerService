"""M0：users、roles（含 3 个角色种子数据）

数据库设计 §3.1–3.2；权限清单与角色映射见安全设计 §2.1。
user_app_roles 外键依赖 apps，随 M1 的 apps 迁移一起创建。

Revision ID: 0001
Revises:
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 迁移不 import 业务代码：权限清单在此固化（与安全设计 §2.1 一致）
ALL_PERMS = [
    "app.read",
    "app.write",
    "app.create",
    "app.delete",
    "app.copy",
    "member.write",
    "kb.read",
    "kb.write",
    "chat.debug",
    "context.compact",
    "key.read",
    "key.write",
    "webhook.write",
    "handoff.release",
    "callback.clear",
    "log.read",
    "feedback.write",
    "eval.read",
    "eval.write",
    "eval.run",
    "stats.read",
    "audit.read",
    "user.admin",
    "system.read",
    "system.write",
]
_NOT_APP_ADMIN = {
    "app.create",
    "app.delete",
    "app.copy",
    "user.admin",
    "system.read",
    "system.write",
}
APP_ADMIN_PERMS = [p for p in ALL_PERMS if p not in _NOT_APP_ADMIN]
VIEWER_PERMS = ["app.read", "kb.read", "chat.debug", "log.read", "eval.read", "stats.read"]

ROLES = [
    {"id": "super_admin", "name": "超级管理员", "permissions": ALL_PERMS},
    {"id": "app_admin", "name": "应用管理员", "permissions": APP_ADMIN_PERMS},
    {"id": "viewer", "name": "只读成员", "permissions": VIEWER_PERMS},
]


def upgrade() -> None:
    ts = sa.DateTime(timezone=True)
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("is_super_admin", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("failed_logins", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("locked_until", ts, nullable=True),
        sa.Column("last_login_at", ts, nullable=True),
        sa.Column("created_at", ts, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", ts, server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", ts, nullable=True),
        sa.CheckConstraint("status IN ('active', 'disabled')", name=op.f("ck_users_status")),
        sa.CheckConstraint("email = lower(email)", name=op.f("ck_users_email_lower")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
    )
    op.create_index(
        "uq_users_email_active",
        "users",
        ["email"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    roles = op.create_table(
        "roles",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "permissions", postgresql.JSONB(), server_default=sa.text("'[]'"), nullable=False
        ),
        sa.Column("created_at", ts, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_roles")),
    )
    op.bulk_insert(roles, ROLES)


def downgrade() -> None:
    op.drop_table("roles")
    op.drop_index("uq_users_email_active", table_name="users")
    op.drop_table("users")
