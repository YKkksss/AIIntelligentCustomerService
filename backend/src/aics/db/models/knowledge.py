"""documents、chunks（数据库设计 §3.6–3.7）。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    text as sa_text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from aics.db.base import Base, CreatedAtMixin, SoftDeleteMixin, TimestampMixin, new_id

DOCUMENT_STATUSES = ("queued", "parsing", "indexing", "done", "failed", "deleting")
QUESTION_STATUSES = ("active", "pending")
QUESTION_SOURCES = ("manual", "llm")
QUESTION_MAX_CHARS = 200
TAG_MAX_CHARS = 50
MAX_TAGS = 20
KB_STATUSES = ("active", "deleting")


class KnowledgeBase(TimestampMixin, Base):
    """知识库（M4.7）：平台公共的一等资源，客服通过 `app_kb_links` 关联使用。"""

    __tablename__ = "knowledge_bases"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'deleting')", name="status"),
        Index("ix_knowledge_bases_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'active'"))
    # 库级切块参数：一个库被多个客服复用时切法必须一致，所以配置下沉到库
    chunk_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa_text("'{}'::jsonb")
    )
    config_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sa_text("1")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))


class AppKBLink(CreatedAtMixin, Base):
    """客服 ↔ 知识库的关联（M4.7）；复合主键，删除客服或库时级联清理。"""

    __tablename__ = "app_kb_links"
    __table_args__ = (Index("ix_app_kb_links_kb", "kb_id"),)

    app_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("apps.id", ondelete="CASCADE"), primary_key=True
    )
    kb_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), primary_key=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))


class Document(TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint("source_type IN ('file', 'url')", name="source_type"),
        CheckConstraint(
            "status IN ('queued', 'parsing', 'indexing', 'done', 'failed', 'deleting')",
            name="status",
        ),
        CheckConstraint("progress BETWEEN 0 AND 100", name="progress"),
        Index(
            "uq_documents_app_hash",
            "app_id",
            "content_hash",
            unique=True,
            postgresql_where=sa_text("deleted_at IS NULL"),
        ),
        Index("ix_documents_app_status", "app_id", "status"),
        Index("ix_documents_app_created", "app_id", sa_text("created_at DESC")),
        Index("ix_documents_kb_status", "kb_id", "status"),
        Index("ix_documents_kb_created", "kb_id", sa_text("created_at DESC")),
        Index("ix_documents_tags", "tags", postgresql_using="gin"),
        Index(
            "ix_documents_expires_at",
            "expires_at",
            postgresql_where=sa_text("expires_at IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    app_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("apps.id"), nullable=False
    )
    # 知识库归属（M4.7）：迁移 0018 回填存量；⑤ 服务全面写入后收紧为 NOT NULL
    kb_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_bases.id")
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_uri: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    content_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    storage_key: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("1"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'queued'"))
    progress: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=sa_text("0"))
    error: Mapped[str | None] = mapped_column(Text)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    index_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    pending_index_version: Mapped[int | None] = mapped_column(Integer)
    chunk_config_version: Mapped[int | None] = mapped_column(Integer)
    # 文档级切块配置覆盖（M1.7）：保存时已与应用配置合并并经 ChunkConfig 校验的完整配置；
    # NULL = 跟随应用 chunk_config。应用 chunk_config 变化不影响有覆盖的文档。
    chunk_config: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    embed_model: Mapped[str | None] = mapped_column(Text)
    # 生效版本向量化时使用的标题（上下文增强前缀）；≠ title 时后台提示重建
    indexed_title: Mapped[str | None] = mapped_column(Text)
    # 有效期（M4.3b）：进 retrievable() 的时间窗；NULL 表示不限
    effective_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 64 位 SimHash：入库近重复提示（只提示不阻止）
    simhash: Mapped[int | None] = mapped_column(BigInteger)
    # Metadata tags for filtered retrieval (M2.2b); mirrored into Qdrant payload `tags`
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=sa_text("'{}'")
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=sa_text("'{}'")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))


class ChunkParent(CreatedAtMixin, Base):
    """父块（切块模式 parent_child）：不向量化、不参与检索，只在子块命中后取其正文送入 LLM。"""

    __tablename__ = "chunk_parents"
    __table_args__ = (UniqueConstraint("document_id", "index_version", "ordinal"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    app_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    index_version: Mapped[int] = mapped_column(Integer, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    heading_path: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    page_from: Mapped[int | None] = mapped_column(Integer)
    page_to: Mapped[int | None] = mapped_column(Integer)


class Chunk(CreatedAtMixin, Base):
    __tablename__ = "chunks"
    __table_args__ = (
        CheckConstraint("review_status IN ('none', 'flagged', 'approved')", name="review_status"),
        CheckConstraint("source IN ('parsed', 'manual', 'edited')", name="source"),
        UniqueConstraint("document_id", "index_version", "ordinal"),
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
        Index("ix_chunks_app_enabled", "app_id", postgresql_where=sa_text("enabled")),
        Index("ix_chunks_kb_enabled", "kb_id", postgresql_where=sa_text("enabled")),
        Index(
            "ix_chunks_parent_id", "parent_id", postgresql_where=sa_text("parent_id IS NOT NULL")
        ),
        Index(
            "ix_chunks_flagged",
            "app_id",
            "review_status",
            postgresql_where=sa_text("review_status = 'flagged'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    app_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # 知识库归属（M4.7）：与 documents.kb_id 由入库流水线保证一致，供检索直接过滤
    kb_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_bases.id")
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    index_version: Mapped[int] = mapped_column(Integer, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    # 父子模式下指向父块；通用模式为 NULL（切块自身正文即送入 LLM 的内容）
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chunk_parents.id", ondelete="CASCADE")
    )
    heading_path: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    search_text: Mapped[str] = mapped_column(Text, nullable=False)
    tsv: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple'::regconfig, search_text)", persisted=True)
    )
    tokens: Mapped[int | None] = mapped_column(Integer)
    page_from: Mapped[int | None] = mapped_column(Integer)
    page_to: Mapped[int | None] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sa_text("true"))
    review_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=sa_text("'none'")
    )
    flag_reason: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 切块来源（M4.3b）：重建只替换 parsed；manual 迁移；edited 按正文哈希匹配，失配降级 manual
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'parsed'"))


class ChunkQuestion(CreatedAtMixin, Base):
    """Supplementary question for a chunk (M2.2b, 数据库设计 §3.7.2, 设计讨论 0003).

    Each row is embedded on its own (Qdrant point id = this id, payload `kind=question`,
    `chunk_id` = the chunk it answers). On a document rebuild the row is re-attached to the
    new build's chunk with the same text hash (`anchor_hash`); when none matches it becomes
    `pending` with `chunk_id` NULL and its point is removed until an admin re-attaches it.
    """

    __tablename__ = "chunk_questions"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'pending')", name="status"),
        CheckConstraint("source IN ('manual', 'llm')", name="source"),
        CheckConstraint(
            f"char_length(question) BETWEEN 1 AND {QUESTION_MAX_CHARS}", name="question_len"
        ),
        Index("ix_chunk_questions_chunk", "chunk_id"),
        Index("ix_chunk_questions_doc", "app_id", "document_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    app_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # 知识库归属（M4.7）：随文档一起迁移，切块级运营按库过滤
    kb_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_bases.id")
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    # SET NULL (not CASCADE): old-build cleanup must not silently drop operator work
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chunks.id", ondelete="SET NULL")
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'active'"))
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default=sa_text("'manual'"))
    anchor_ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    anchor_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)  # sha256(chunk text)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
