"""Platform settings (`system_settings`, 数据库设计 §3.14).

v1 has one key, `default_session_config`, which is the platform default merged into each
app's `session_config` (会话生命周期与转人工 §2.1). The table is not app-scoped; only
super_admin may write it (enforced by the admin API in M4.2, which also writes the audit log).
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from aics.core.errors import ValidationAppError
from aics.db.models import SystemSetting
from aics.rag.config import PLATFORM_DEFAULT_SESSION, EffectiveSessionConfig

log = logging.getLogger(__name__)

DEFAULT_SESSION_CONFIG_KEY = "default_session_config"
KNOWN_KEYS = frozenset({DEFAULT_SESSION_CONFIG_KEY})


class SystemSettingsRepository:
    """Plain repository (no app_id): platform-wide rows keyed by a fixed whitelist."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_value(self, key: str) -> Any | None:
        row = await self.session.get(SystemSetting, key)
        return None if row is None else row.value

    async def set_value(self, key: str, value: Any, *, updated_by: UUID | None) -> None:
        if key not in KNOWN_KEYS:
            raise ValidationAppError(message=f"未知的系统设置项：{key}")
        stmt = insert(SystemSetting).values(key=key, value=value, updated_by=updated_by)
        stmt = stmt.on_conflict_do_update(
            index_elements=[SystemSetting.key],
            set_={"value": stmt.excluded.value, "updated_by": updated_by, "updated_at": func.now()},
        )
        await self.session.execute(stmt)


class SystemSettingsService:
    def __init__(self, session: AsyncSession) -> None:
        self.repo = SystemSettingsRepository(session)

    async def get_default_session_config(self) -> EffectiveSessionConfig:
        """Stored platform default; falls back to the code default if missing or invalid.

        Falling back (and logging) rather than failing keeps chat available if the row was
        damaged manually; the admin page re-validates on save.
        """
        raw = await self.repo.get_value(DEFAULT_SESSION_CONFIG_KEY)
        if raw is None:
            return PLATFORM_DEFAULT_SESSION
        try:
            return EffectiveSessionConfig.model_validate(raw)
        except ValidationError:
            log.warning("system_settings.default_session_config invalid; using code default")
            return PLATFORM_DEFAULT_SESSION

    async def set_default_session_config(
        self, value: dict[str, Any], *, updated_by: UUID | None
    ) -> EffectiveSessionConfig:
        try:
            cfg = EffectiveSessionConfig.model_validate(value)
        except ValidationError as e:
            raise ValidationAppError(
                message="默认会话设置校验失败",
                details={
                    "errors": [
                        {
                            "loc": ["default_session_config", *map(str, err["loc"])],
                            "msg": err["msg"],
                        }
                        for err in e.errors()
                    ]
                },
            ) from e
        await self.repo.set_value(
            DEFAULT_SESSION_CONFIG_KEY, cfg.model_dump(mode="json"), updated_by=updated_by
        )
        return cfg
