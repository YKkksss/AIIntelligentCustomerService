"""knowledge bases and app links (M4.7 ②)

为每个应用生成「<应用名> 默认知识库」并把存量文档 / 切块 / 补充问题回填 `kb_id`，
同时建立 `app_kb_links` 关联表。`kb_id` 先保持可空，待 ⑤ 服务层全面写入后再收紧为
NOT NULL（避免这一步让全量集成用例的新建文档失败）。

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_bases",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        sa.Column("chunk_config", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("config_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_by", sa.UUID(as_uuid=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_knowledge_bases_created_by_users")
        ),
        sa.CheckConstraint(
            "status IN ('active', 'deleting')", name=op.f("ck_knowledge_bases_status")
        ),
    )
    op.create_index(op.f("ix_knowledge_bases_status"), "knowledge_bases", ["status"])

    op.create_table(
        "app_kb_links",
        sa.Column("app_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("kb_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by", sa.UUID(as_uuid=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["app_id"],
            ["apps.id"],
            name=op.f("fk_app_kb_links_app_id_apps"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["kb_id"],
            ["knowledge_bases.id"],
            name=op.f("fk_app_kb_links_kb_id_knowledge_bases"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_app_kb_links_created_by_users")
        ),
        sa.PrimaryKeyConstraint("app_id", "kb_id", name=op.f("pk_app_kb_links")),
    )
    op.create_index(op.f("ix_app_kb_links_kb"), "app_kb_links", ["kb_id"])

    op.add_column("documents", sa.Column("kb_id", sa.UUID(as_uuid=True)))
    op.add_column("chunks", sa.Column("kb_id", sa.UUID(as_uuid=True)))
    op.add_column(
        "chunk_questions",
        sa.Column("kb_id", sa.UUID(as_uuid=True)),
    )

    # 每个应用一条默认知识库，并把存量数据按 app 回填到对应库
    op.execute(
        """
        CREATE TEMP TABLE _kb_map ON COMMIT DROP AS
        SELECT id AS app_id, gen_random_uuid() AS kb_id FROM apps
        """
    )
    op.execute(
        """
        INSERT INTO knowledge_bases (id, name, description, chunk_config, config_version)
        SELECT m.kb_id,
               a.name || ' 默认知识库',
               '迁移 0018 自动创建：原属于该智能客服的知识内容',
               COALESCE(a.chunk_config, '{}'::jsonb),
               1
        FROM _kb_map m
        JOIN apps a ON a.id = m.app_id
        """
    )
    op.execute("INSERT INTO app_kb_links (app_id, kb_id) SELECT app_id, kb_id FROM _kb_map")
    op.execute(
        """
        UPDATE documents d SET kb_id = m.kb_id
        FROM _kb_map m WHERE d.app_id = m.app_id
        """
    )
    op.execute("UPDATE chunks c SET kb_id = d.kb_id FROM documents d WHERE c.document_id = d.id")
    op.execute(
        """
        UPDATE chunk_questions q SET kb_id = d.kb_id
        FROM documents d WHERE q.document_id = d.id
        """
    )

    op.create_index(op.f("ix_documents_kb_status"), "documents", ["kb_id", "status"])
    op.create_index(
        op.f("ix_documents_kb_created"),
        "documents",
        ["kb_id", sa.text("created_at DESC")],
    )
    op.create_index(
        op.f("ix_chunks_kb_enabled"),
        "chunks",
        ["kb_id"],
        postgresql_where=sa.text("enabled"),
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_chunks_kb_enabled"), table_name="chunks")
    op.drop_index(op.f("ix_documents_kb_created"), table_name="documents")
    op.drop_index(op.f("ix_documents_kb_status"), table_name="documents")
    op.drop_column("chunk_questions", "kb_id")
    op.drop_column("chunks", "kb_id")
    op.drop_column("documents", "kb_id")
    op.drop_index(op.f("ix_app_kb_links_kb"), table_name="app_kb_links")
    op.drop_table("app_kb_links")
    op.drop_index(op.f("ix_knowledge_bases_status"), table_name="knowledge_bases")
    op.drop_table("knowledge_bases")
