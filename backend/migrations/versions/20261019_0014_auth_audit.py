"""M4.1 ②: audit_logs 与 users 的登录态列（数据库设计 §3.13、§3.1）

`audit_logs` 只追加：`id` 用 IDENTITY 自增；`app_id` 与 `actor_id` **不建外键**——审计要能在
应用或用户被删除后仍然解释「谁在什么时候改了什么」，外键的 CASCADE/SET NULL 会抹掉这段历史。
`users` 增加两列：`must_change_password`（首次登录强制改密）与 `token_version`（改密 / 禁用时
+1，让旧 access / refresh token 立即失效）。

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACTOR_TYPES = ("user", "api_key", "system")


def _check(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "must_change_password", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )
    op.add_column(
        "users",
        sa.Column("token_version", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.UUID(), nullable=True),
        sa.Column("app_id", sa.UUID(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=True),
        sa.Column("target_id", sa.UUID(), nullable=True),
        sa.Column("diff", postgresql.JSONB(), nullable=True),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("request_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            _check("actor_type", ACTOR_TYPES), name=op.f("ck_audit_logs_actor_type")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_logs")),
    )
    op.create_index(
        "ix_audit_logs_app_created",
        "audit_logs",
        ["app_id", sa.text("created_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_audit_logs_actor_created",
        "audit_logs",
        ["actor_id", sa.text("created_at DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_audit_logs_actor_created", table_name="audit_logs")
    op.drop_index("ix_audit_logs_app_created", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_column("users", "token_version")
    op.drop_column("users", "must_change_password")
