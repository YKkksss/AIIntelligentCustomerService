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
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from aics.db.base import Base, CreatedAtMixin, SoftDeleteMixin, TimestampMixin, new_id

DOCUMENT_STATUSES = ("queued", "parsing", "indexing", "done", "failed", "deleting")


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
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    app_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("apps.id"), nullable=False
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
    embed_model: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=sa_text("'{}'")
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))


class Chunk(CreatedAtMixin, Base):
    __tablename__ = "chunks"
    __table_args__ = (
        CheckConstraint("review_status IN ('none', 'flagged', 'approved')", name="review_status"),
        UniqueConstraint("document_id", "index_version", "ordinal"),
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
        Index("ix_chunks_app_enabled", "app_id", postgresql_where=sa_text("enabled")),
        Index(
            "ix_chunks_flagged",
            "app_id",
            "review_status",
            postgresql_where=sa_text("review_status = 'flagged'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    app_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    index_version: Mapped[int] = mapped_column(Integer, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
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
