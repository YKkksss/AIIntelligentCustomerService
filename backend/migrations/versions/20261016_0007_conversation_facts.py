"""M2.1: conversation_facts + conversation_fact_history (设计讨论 0002, moved up from M5)

数据库设计 §3.9.1–3.9.2; 对话上下文管理 §5.3. The history table is append-only: a trigger rejects
UPDATE (rows only disappear through ON DELETE CASCADE when the fact / conversation is deleted).

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-16 16:25:34.986516+08:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APPEND_ONLY_FUNC = """
CREATE OR REPLACE FUNCTION aics_fact_history_no_update() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'conversation_fact_history is append-only'
    USING ERRCODE = 'restrict_violation';
END;
$$
"""
APPEND_ONLY_TRIGGER = """
CREATE TRIGGER trg_conversation_fact_history_no_update
BEFORE UPDATE ON conversation_fact_history
FOR EACH ROW EXECUTE FUNCTION aics_fact_history_no_update()
"""


def upgrade() -> None:
    op.create_table(
        "conversation_facts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=False),
        sa.Column("app_id", sa.UUID(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("source_message_id", sa.UUID(), nullable=True),
        sa.Column("confidence", sa.REAL(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("importance", sa.SmallInteger(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("retracted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "category IN ('order', 'identity', 'product', 'issue', 'resolution', 'emotion', 'other')",
            name=op.f("ck_conversation_facts_category"),
        ),
        sa.CheckConstraint(
            "key ~ '^(order_no|product|issue_type|phone|address_region|solution_given|user_emotion|other:[a-z_]{1,32})$'",
            name=op.f("ck_conversation_facts_key"),
        ),
        sa.CheckConstraint(
            "source IN ('slot', 'compaction')", name=op.f("ck_conversation_facts_source")
        ),
        sa.CheckConstraint(
            "confidence BETWEEN 0 AND 1", name=op.f("ck_conversation_facts_confidence")
        ),
        sa.CheckConstraint(
            "importance BETWEEN 1 AND 3", name=op.f("ck_conversation_facts_importance")
        ),
        sa.CheckConstraint("length(value) > 0", name=op.f("ck_conversation_facts_value")),
        sa.CheckConstraint("version >= 1", name=op.f("ck_conversation_facts_version")),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_conversation_facts_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversation_facts")),
        sa.UniqueConstraint(
            "conversation_id", "key", name=op.f("uq_conversation_facts_conversation_id_key")
        ),
    )
    op.create_index(
        "ix_conversation_facts_app_conversation",
        "conversation_facts",
        ["app_id", "conversation_id"],
        unique=False,
    )
    op.create_table(
        "conversation_fact_history",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("fact_id", sa.UUID(), nullable=False),
        sa.Column("app_id", sa.UUID(), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("op", sa.Text(), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column("old_source_message_id", sa.UUID(), nullable=True),
        sa.Column("new_source_message_id", sa.UUID(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("confidence", sa.REAL(), nullable=True),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "op IN ('add', 'update', 'retract')", name=op.f("ck_conversation_fact_history_op")
        ),
        sa.CheckConstraint(
            "(op = 'add' AND old_value IS NULL AND new_value IS NOT NULL) OR (op = 'update' AND old_value IS NOT NULL AND new_value IS NOT NULL) OR (op = 'retract' AND new_value IS NULL)",
            name=op.f("ck_conversation_fact_history_op_values"),
        ),
        sa.CheckConstraint(
            "source IN ('slot', 'compaction')", name=op.f("ck_conversation_fact_history_source")
        ),
        sa.CheckConstraint(
            "confidence BETWEEN 0 AND 1", name=op.f("ck_conversation_fact_history_confidence")
        ),
        sa.ForeignKeyConstraint(
            ["fact_id"],
            ["conversation_facts.id"],
            name=op.f("fk_conversation_fact_history_fact_id_conversation_facts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversation_fact_history")),
    )
    op.create_index(
        "ix_conversation_fact_history_app_conversation_changed",
        "conversation_fact_history",
        ["app_id", "conversation_id", "changed_at"],
        unique=False,
    )
    op.execute(APPEND_ONLY_FUNC)
    op.execute(APPEND_ONLY_TRIGGER)


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_conversation_fact_history_no_update ON conversation_fact_history"
    )
    op.execute("DROP FUNCTION IF EXISTS aics_fact_history_no_update()")
    op.drop_table("conversation_fact_history")
    op.drop_table("conversation_facts")
