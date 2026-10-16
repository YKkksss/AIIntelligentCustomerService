"""M1.2：apps、user_app_roles、documents、chunks、jobs

数据库设计 §3.3–3.7、§3.12。配置 jsonb 列的结构由 aics.rag.config 定义，迁移不写默认值（由 apps.service 写入）。

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-16 12:49:54.463960+08:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "apps",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("greeting", sa.Text(), nullable=True),
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.Column("answer_style", sa.Text(), server_default=sa.text("'concise'"), nullable=False),
        sa.Column(
            "fallback_text",
            sa.Text(),
            server_default=sa.text("'抱歉，这个问题我暂时无法回答，建议联系人工客服。'"),
            nullable=False,
        ),
        sa.Column("language", sa.Text(), server_default=sa.text("'zh-CN'"), nullable=False),
        sa.Column("rag_config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("handoff_config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "session_config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "webhook_config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("chunk_config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "custom_dict",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("config_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "chunk_config_version", sa.Integer(), server_default=sa.text("1"), nullable=False
        ),
        sa.Column("created_by", sa.UUID(), nullable=True),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "answer_style IN ('concise', 'detailed', 'custom')", name=op.f("ck_apps_answer_style")
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled', 'deleting')", name=op.f("ck_apps_status")
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_apps_created_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_apps")),
    )
    op.create_table(
        "documents",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("app_id", sa.UUID(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=True),
        sa.Column("mime_type", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("content_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("progress", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("index_version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("pending_index_version", sa.Integer(), nullable=True),
        sa.Column("chunk_config_version", sa.Integer(), nullable=True),
        sa.Column("embed_model", sa.Text(), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("created_by", sa.UUID(), nullable=True),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("source_type IN ('file', 'url')", name=op.f("ck_documents_source_type")),
        sa.CheckConstraint(
            "status IN ('queued', 'parsing', 'indexing', 'done', 'failed', 'deleting')",
            name=op.f("ck_documents_status"),
        ),
        sa.CheckConstraint("progress BETWEEN 0 AND 100", name=op.f("ck_documents_progress")),
        sa.ForeignKeyConstraint(["app_id"], ["apps.id"], name=op.f("fk_documents_app_id_apps")),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_documents_created_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
    )
    op.create_index(
        "ix_documents_app_created",
        "documents",
        ["app_id", sa.literal_column("created_at DESC")],
        unique=False,
    )
    op.create_index("ix_documents_app_status", "documents", ["app_id", "status"], unique=False)
    op.create_index(
        "uq_documents_app_hash",
        "documents",
        ["app_id", "content_hash"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("app_id", sa.UUID(), nullable=True),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("target_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("progress", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("celery_task_id", sa.Text(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'done', 'failed', 'cancelled')",
            name=op.f("ck_jobs_status"),
        ),
        sa.CheckConstraint(
            "type IN ('ingest', 'replace', 'reindex_document', 'reindex_app', 'reindex_all', 'retokenize_app', 'cleanup_document', 'cleanup_app', 'compact', 'reconcile', 'eval')",
            name=op.f("ck_jobs_type"),
        ),
        sa.CheckConstraint("progress BETWEEN 0 AND 100", name=op.f("ck_jobs_progress")),
        sa.ForeignKeyConstraint(
            ["app_id"], ["apps.id"], name=op.f("fk_jobs_app_id_apps"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_jobs_created_by_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
    )
    op.create_index(
        "ix_jobs_app_created",
        "jobs",
        ["app_id", sa.literal_column("created_at DESC")],
        unique=False,
    )
    op.create_index("ix_jobs_status", "jobs", ["status"], unique=False)
    op.create_table(
        "user_app_roles",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("app_id", sa.UUID(), nullable=False),
        sa.Column("role_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role_id IN ('app_admin', 'viewer')", name=op.f("ck_user_app_roles_role_id")
        ),
        sa.ForeignKeyConstraint(
            ["app_id"], ["apps.id"], name=op.f("fk_user_app_roles_app_id_apps"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["role_id"], ["roles.id"], name=op.f("fk_user_app_roles_role_id_roles")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_app_roles_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "app_id", name=op.f("pk_user_app_roles")),
    )
    op.create_index("ix_user_app_roles_app_id", "user_app_roles", ["app_id"], unique=False)
    op.create_table(
        "chunks",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("app_id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("index_version", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("heading_path", sa.Text(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('simple'::regconfig, search_text)", persisted=True),
            nullable=False,
        ),
        sa.Column("tokens", sa.Integer(), nullable=True),
        sa.Column("page_from", sa.Integer(), nullable=True),
        sa.Column("page_to", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("review_status", sa.Text(), server_default=sa.text("'none'"), nullable=False),
        sa.Column("flag_reason", sa.Text(), nullable=True),
        sa.Column("reviewed_by", sa.UUID(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "review_status IN ('none', 'flagged', 'approved')", name=op.f("ck_chunks_review_status")
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_chunks_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chunks")),
        sa.UniqueConstraint(
            "document_id",
            "index_version",
            "ordinal",
            name=op.f("uq_chunks_document_id_index_version_ordinal"),
        ),
    )
    op.create_index(
        "ix_chunks_app_enabled",
        "chunks",
        ["app_id"],
        unique=False,
        postgresql_where=sa.text("enabled"),
    )
    op.create_index(
        "ix_chunks_flagged",
        "chunks",
        ["app_id", "review_status"],
        unique=False,
        postgresql_where=sa.text("review_status = 'flagged'"),
    )
    op.create_index("ix_chunks_tsv", "chunks", ["tsv"], unique=False, postgresql_using="gin")


def downgrade() -> None:
    op.drop_index("ix_chunks_tsv", table_name="chunks", postgresql_using="gin")
    op.drop_index(
        "ix_chunks_flagged",
        table_name="chunks",
        postgresql_where=sa.text("review_status = 'flagged'"),
    )
    op.drop_index("ix_chunks_app_enabled", table_name="chunks", postgresql_where=sa.text("enabled"))
    op.drop_table("chunks")
    op.drop_index("ix_user_app_roles_app_id", table_name="user_app_roles")
    op.drop_table("user_app_roles")
    op.drop_index("ix_jobs_status", table_name="jobs")
    op.drop_index("ix_jobs_app_created", table_name="jobs")
    op.drop_table("jobs")
    op.drop_index(
        "uq_documents_app_hash",
        table_name="documents",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.drop_index("ix_documents_app_status", table_name="documents")
    op.drop_index("ix_documents_app_created", table_name="documents")
    op.drop_table("documents")
    op.drop_table("apps")
