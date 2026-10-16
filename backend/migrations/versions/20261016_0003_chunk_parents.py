"""M1.3b：父子切块 —— chunk_parents 表与 chunks.parent_id

数据库设计 §3.7.1；父块不向量化、不参与检索，检索命中子块后按 parent_id 取父块正文送入 LLM。

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-16 13:38:27.101425+08:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chunk_parents",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("app_id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("index_version", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("heading_path", sa.Text(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("tokens", sa.Integer(), nullable=False),
        sa.Column("page_from", sa.Integer(), nullable=True),
        sa.Column("page_to", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_chunk_parents_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chunk_parents")),
        sa.UniqueConstraint(
            "document_id",
            "index_version",
            "ordinal",
            name=op.f("uq_chunk_parents_document_id_index_version_ordinal"),
        ),
    )
    op.add_column("chunks", sa.Column("parent_id", sa.UUID(), nullable=True))
    op.create_index(
        "ix_chunks_parent_id",
        "chunks",
        ["parent_id"],
        unique=False,
        postgresql_where=sa.text("parent_id IS NOT NULL"),
    )
    op.create_foreign_key(
        op.f("fk_chunks_parent_id_chunk_parents"),
        "chunks",
        "chunk_parents",
        ["parent_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("fk_chunks_parent_id_chunk_parents"), "chunks", type_="foreignkey")
    op.drop_index(
        "ix_chunks_parent_id",
        table_name="chunks",
        postgresql_where=sa.text("parent_id IS NOT NULL"),
    )
    op.drop_column("chunks", "parent_id")
    op.drop_table("chunk_parents")
