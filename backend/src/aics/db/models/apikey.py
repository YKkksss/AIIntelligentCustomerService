"""api_keys (数据库设计 §3.5): open-API credentials, one app per key.

Only `prefix` (first 12 characters, for lookup and display) and `key_hash`
(HMAC-SHA256(pepper, full key), hex) are stored; the plaintext is shown once (安全设计 §1).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, Text, func
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import ARRAY, CIDR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from aics.db.base import Base, new_id

API_KEY_SCOPES = ("chat", "kb:read", "kb:write", "handoff:write")


class ApiKey(Base):
    __tablename__ = "api_keys"
    __table_args__ = (
        CheckConstraint(
            f"scopes <@ ARRAY[{', '.join(repr(s) for s in API_KEY_SCOPES)}]::text[]",
            name="scopes",
        ),
        CheckConstraint("cardinality(scopes) > 0", name="scopes_not_empty"),
        CheckConstraint("rpm_limit > 0", name="rpm_limit"),
        CheckConstraint("daily_quota IS NULL OR daily_quota > 0", name="daily_quota"),
        Index("ix_api_keys_app_id", "app_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_id)
    app_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("apps.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    prefix: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    key_hash: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    rpm_limit: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("60"))
    daily_quota: Mapped[int | None] = mapped_column(Integer)
    ip_allowlist: Mapped[list[str] | None] = mapped_column(ARRAY(CIDR))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
