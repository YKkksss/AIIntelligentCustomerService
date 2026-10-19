"""M3.5: conversation_events + webhook_deliveries (ADR-0016, 数据库设计 §3.15–3.16)

会话事件 outbox：状态变更与事件同事务写入 `conversation_events`（`seq` 由
`conversations.event_seq` 分配），`webhook_deliveries` 是每个事件最多一条的投递记录
（`id` = `event_id` = payload 的 `id`，接入方按它去重；重试 10s/30s/2min/10min/30min）。

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

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


def _check(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.create_table(
        "conversation_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("app_id", sa.UUID(), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("data", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(_check("type", EVENT_TYPES), name=op.f("ck_conversation_events_type")),
        sa.ForeignKeyConstraint(
            ["app_id"],
            ["apps.id"],
            name=op.f("fk_conversation_events_app_id_apps"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_conversation_events_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversation_events")),
        sa.UniqueConstraint(
            "conversation_id", "seq", name=op.f("uq_conversation_events_conversation_id_seq")
        ),
    )
    op.create_index(
        "ix_conversation_events_app_created",
        "conversation_events",
        ["app_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("event_id", sa.UUID(), nullable=False),
        sa.Column("app_id", sa.UUID(), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=True),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("payload", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_status_code", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_webhook_deliveries_attempts")),
        sa.CheckConstraint(
            _check("status", DELIVERY_STATUSES), name=op.f("ck_webhook_deliveries_status")
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["conversation_events.id"],
            name=op.f("fk_webhook_deliveries_event_id_conversation_events"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_deliveries")),
        sa.UniqueConstraint("event_id", name=op.f("uq_webhook_deliveries_event_id")),
    )
    op.create_index(
        "ix_webhook_deliveries_app_created",
        "webhook_deliveries",
        ["app_id", sa.text("created_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_webhook_deliveries_status_next_attempt",
        "webhook_deliveries",
        ["status", "next_attempt_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_webhook_deliveries_status_next_attempt", table_name="webhook_deliveries")
    op.drop_index("ix_webhook_deliveries_app_created", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")
    op.drop_index("ix_conversation_events_app_created", table_name="conversation_events")
    op.drop_table("conversation_events")
