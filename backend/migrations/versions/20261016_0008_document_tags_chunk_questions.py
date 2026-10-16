"""M2.2b：documents.tags（过滤检索）与 chunk_questions（切块补充问题）

数据库设计 §3.6 / §3.7.2；RAG 流程设计 §4.2；设计讨论 0003。

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column(
            "tags", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False
        ),
    )
    op.create_index("ix_documents_tags", "documents", ["tags"], postgresql_using="gin")
    op.create_table(
        "chunk_questions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("app_id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("chunk_id", sa.UUID(), nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("source", sa.Text(), server_default=sa.text("'manual'"), nullable=False),
        sa.Column("anchor_ordinal", sa.Integer(), nullable=False),
        sa.Column("anchor_hash", sa.CHAR(64), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'pending')", name=op.f("ck_chunk_questions_status")
        ),
        sa.CheckConstraint("source IN ('manual', 'llm')", name=op.f("ck_chunk_questions_source")),
        sa.CheckConstraint(
            "char_length(question) BETWEEN 1 AND 200", name=op.f("ck_chunk_questions_question_len")
        ),
        sa.CheckConstraint(
            "status = 'pending' OR chunk_id IS NOT NULL",
            name=op.f("ck_chunk_questions_active_has_chunk"),
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id"],
            ["chunks.id"],
            name=op.f("fk_chunk_questions_chunk_id_chunks"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_chunk_questions_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_chunk_questions_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chunk_questions")),
    )
    op.create_index("ix_chunk_questions_chunk", "chunk_questions", ["chunk_id"])
    op.create_index("ix_chunk_questions_doc", "chunk_questions", ["app_id", "document_id"])


def downgrade() -> None:
    op.drop_table("chunk_questions")
    op.drop_index("ix_documents_tags", table_name="documents")
    op.drop_column("documents", "tags")
