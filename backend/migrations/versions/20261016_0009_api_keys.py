"""M2.6：api_keys（开放接口鉴权，M3.1 核心提前）

数据库设计 §3.5；安全设计 §1；接口规范 §2 / §6。

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("app_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("prefix", sa.Text(), nullable=False),
        sa.Column("key_hash", sa.Text(), nullable=False),
        sa.Column("scopes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("rpm_limit", sa.Integer(), server_default=sa.text("60"), nullable=False),
        sa.Column("daily_quota", sa.Integer(), nullable=True),
        sa.Column("ip_allowlist", postgresql.ARRAY(postgresql.CIDR()), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "scopes <@ ARRAY['chat', 'kb:read', 'kb:write', 'handoff:write']::text[]",
            name=op.f("ck_api_keys_scopes"),
        ),
        sa.CheckConstraint("cardinality(scopes) > 0", name=op.f("ck_api_keys_scopes_not_empty")),
        sa.CheckConstraint("rpm_limit > 0", name=op.f("ck_api_keys_rpm_limit")),
        sa.CheckConstraint(
            "daily_quota IS NULL OR daily_quota > 0", name=op.f("ck_api_keys_daily_quota")
        ),
        sa.ForeignKeyConstraint(["app_id"], ["apps.id"], name=op.f("fk_api_keys_app_id_apps")),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_api_keys_created_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_keys")),
        sa.UniqueConstraint("prefix", name=op.f("uq_api_keys_prefix")),
    )
    op.create_index("ix_api_keys_app_id", "api_keys", ["app_id"])


def downgrade() -> None:
    op.drop_index("ix_api_keys_app_id", table_name="api_keys")
    op.drop_table("api_keys")
