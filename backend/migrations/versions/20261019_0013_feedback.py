"""M3.6 ②: feedback（数据库设计 §3.10）

一条 assistant 回答最多一条反馈，`message_id` 唯一；重复提交覆盖写（`ON CONFLICT DO
UPDATE`），`id` 与 `created_at` 不变。`eval_case_id` 是 M5.3 的占位列——`eval_cases` 要到
M5.1 才建表，所以本迁移**不建外键**（M5.3 的迁移补）。

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RATINGS = (1, -1)
SOURCES = ("api", "admin")


def _check(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.create_table(
        "feedback",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("app_id", sa.UUID(), nullable=False),
        sa.Column("message_id", sa.UUID(), nullable=False),
        sa.Column("rating", sa.SmallInteger(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), server_default=sa.text("'api'"), nullable=False),
        sa.Column("handled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("eval_case_id", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(_check("rating", RATINGS), name=op.f("ck_feedback_rating")),
        sa.CheckConstraint(_check("source", SOURCES), name=op.f("ck_feedback_source")),
        sa.ForeignKeyConstraint(
            ["app_id"], ["apps.id"], name=op.f("fk_feedback_app_id_apps"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name=op.f("fk_feedback_message_id_messages"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feedback")),
        # 每条消息一个反馈；重复提交靠它走覆盖写。
        sa.UniqueConstraint("message_id", name=op.f("uq_feedback_message_id")),
    )
    op.create_index(
        "ix_feedback_app_created", "feedback", ["app_id", sa.text("created_at DESC")], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_feedback_app_created", table_name="feedback")
    op.drop_table("feedback")
