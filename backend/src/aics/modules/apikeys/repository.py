"""api_keys repository (M2.6; 数据库设计 §3.5)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, update

from aics.db.models import ApiKey, App
from aics.db.scoped import AppScopedRepository


class ApiKeyRepository(AppScopedRepository[ApiKey]):
    model = ApiKey

    async def list_for_app(self, app_id: UUID) -> list[ApiKey]:
        stmt = self._scoped(app_id).order_by(ApiKey.created_at.desc())
        return list((await self.session.execute(stmt)).scalars())

    async def revoke(self, app_id: UUID, key_id: UUID, *, at: datetime | None = None) -> str | None:
        """Set revoked_at once; returns the key prefix (for cache invalidation) or None."""
        stmt = (
            update(ApiKey)
            .where(ApiKey.app_id == app_id, ApiKey.id == key_id, ApiKey.revoked_at.is_(None))
            .values(revoked_at=at or func.now())
            .returning(ApiKey.prefix)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def revoke_all(self, app_id: UUID) -> list[str]:
        """App deletion (安全设计 §2): revoke every live key of the app; returns prefixes."""
        stmt = (
            update(ApiKey)
            .where(ApiKey.app_id == app_id, ApiKey.revoked_at.is_(None))
            .values(revoked_at=func.now())
            .returning(ApiKey.prefix)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def get_by_prefix_all_apps(self, prefix: str) -> tuple[ApiKey, str] | None:
        """Authentication lookup before the app is known: (key, apps.status)."""
        stmt = (
            select(ApiKey, App.status)
            .join(App, App.id == ApiKey.app_id)
            .where(ApiKey.prefix == prefix, App.deleted_at.is_(None))
        )
        row = (await self.session.execute(stmt)).one_or_none()
        return (row[0], row[1]) if row else None
