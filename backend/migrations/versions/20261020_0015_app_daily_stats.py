"""M4.6 ②: app_daily_stats（数据库设计 §3.12.1）

一天一行、一个应用一行：主键 `(app_id, date)`，`date` 是 Asia/Shanghai 的自然日。写入方只有
`modules.stats` 的聚合任务，重复执行按 `ON CONFLICT (app_id, date) DO UPDATE` 覆盖，幂等。
计数列默认 0；分位数列可为 NULL（当天无样本）。

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COUNTS = (
    "conversations",
    "messages",
    "answered",
    "refused",
    "handoff",
    "errors",
    "handoff_requested",
    "handoff_accepted",
    "handoff_released",
    "handoff_queue_timeout",
    "handoff_offline",
    "handoff_accepted_idle_closed",
    "human_agent_messages",
    "idle_closed",
    "negative_feedback",
)


def upgrade() -> None:
    op.create_table(
        "app_daily_stats",
        sa.Column("app_id", sa.UUID(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        *[
            sa.Column(name, sa.Integer(), server_default=sa.text("0"), nullable=False)
            for name in COUNTS
        ],
        sa.Column("handoff_accept_p50_s", sa.Integer(), nullable=True),
        sa.Column(
            "intent_distribution",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("tokens_in", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("tokens_out", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("latency_p50_ms", sa.Integer(), nullable=True),
        sa.Column("latency_p95_ms", sa.Integer(), nullable=True),
        sa.Column("first_token_p95_ms", sa.Integer(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["app_id"], ["apps.id"], name=op.f("fk_app_daily_stats_app_id_apps"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("app_id", "date", name=op.f("pk_app_daily_stats")),
    )


def downgrade() -> None:
    op.drop_table("app_daily_stats")
