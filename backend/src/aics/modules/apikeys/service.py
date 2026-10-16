"""API key management and open-API authentication (接口规范 §2, 安全设计 §1–§2).

`ApiKeyService` creates / revokes keys (the plaintext is returned once). `ApiKeyAuthenticator`
implements `core.security.Authenticator`: bearer parsing → cached lookup by prefix
(`{prefix}apikey:{key_prefix}`, 60 s, deleted on revoke) → HMAC check → revoked / expired →
app binding and status → scope → rate limits.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from redis.exceptions import RedisError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aics.core.errors import (
    AppError,
    AppNotFoundError,
    ForbiddenError,
    UnauthenticatedError,
    ValidationAppError,
)
from aics.core.security import Principal
from aics.db.models import ApiKey
from aics.db.models.apikey import API_KEY_SCOPES
from aics.modules.apikeys import keys
from aics.modules.apikeys.ratelimit import Limits, RateLimiter
from aics.modules.apikeys.repository import ApiKeyRepository

log = structlog.get_logger()

AUTH_CACHE_TTL_S = 60  # 安全设计 §1
DEFAULT_RPM_LIMIT = 60  # 接口规范 §6
NAME_MAX_CHARS = 100
_CREATE_ATTEMPTS = 3  # prefix collision retries (7 base62 chars; practically never)


class ApiKeyExpiredError(AppError):
    code, status, message = "API_KEY_EXPIRED", 401, "API Key 已过期"


class ApiKeyRevokedError(AppError):
    code, status, message = "API_KEY_REVOKED", 401, "API Key 已吊销"


class AppDisabledError(AppError):
    code, status, message = "APP_DISABLED", 403, "应用已停用"


def cache_key(redis_prefix: str, key_prefix: str) -> str:
    return f"{redis_prefix}apikey:{key_prefix}"


class ApiKeyService:
    def __init__(
        self, session: AsyncSession, *, pepper: str, redis: Any = None, redis_prefix: str = "aics:"
    ) -> None:
        self.session = session
        self.repo = ApiKeyRepository(session)
        self.pepper = pepper
        self.redis = redis
        self.redis_prefix = redis_prefix

    async def create(
        self,
        app_id: UUID,
        *,
        name: str,
        scopes: Sequence[str],
        rpm_limit: int = DEFAULT_RPM_LIMIT,
        daily_quota: int | None = None,
        expires_at: datetime | None = None,
        created_by: UUID | None = None,
    ) -> tuple[ApiKey, str]:
        """Returns (row, plaintext key). The plaintext is never stored or logged."""
        errors = []
        if not name.strip() or len(name) > NAME_MAX_CHARS:
            errors.append({"loc": ["name"], "msg": f"1–{NAME_MAX_CHARS} 字"})
        bad = [s for s in scopes if s not in API_KEY_SCOPES]
        if not scopes or bad:
            errors.append({"loc": ["scopes"], "msg": f"应为 {'/'.join(API_KEY_SCOPES)} 的非空子集"})
        if rpm_limit < 1:
            errors.append({"loc": ["rpm_limit"], "msg": "必须 ≥ 1"})
        if daily_quota is not None and daily_quota < 1:
            errors.append({"loc": ["daily_quota"], "msg": "必须 ≥ 1 或为空"})
        if errors:
            raise ValidationAppError(details={"errors": errors})
        for attempt in range(_CREATE_ATTEMPTS):
            plain = keys.generate_key()
            row = ApiKey(
                app_id=app_id,
                name=name.strip(),
                prefix=keys.key_prefix(plain),
                key_hash=keys.hash_key(plain, self.pepper),
                scopes=list(dict.fromkeys(scopes)),
                rpm_limit=rpm_limit,
                daily_quota=daily_quota,
                expires_at=expires_at,
                created_by=created_by,
            )
            try:
                async with self.session.begin_nested():
                    await self.repo.add(app_id, row)
            except IntegrityError:
                if attempt == _CREATE_ATTEMPTS - 1:
                    raise
                continue
            log.info("apikeys.created", app_id=str(app_id), prefix=row.prefix)
            return row, plain
        raise AssertionError("unreachable")  # pragma: no cover

    async def revoke(self, app_id: UUID, key_id: UUID) -> bool:
        prefix = await self.repo.revoke(app_id, key_id)
        if prefix is not None:
            await self._invalidate([prefix])
        return prefix is not None

    async def revoke_all(self, app_id: UUID) -> int:
        prefixes = await self.repo.revoke_all(app_id)
        await self._invalidate(prefixes)
        return len(prefixes)

    async def _invalidate(self, prefixes: Sequence[str]) -> None:
        if self.redis is None or not prefixes:
            return
        try:
            await self.redis.delete(*[cache_key(self.redis_prefix, p) for p in prefixes])
        except RedisError as e:  # the entry expires within AUTH_CACHE_TTL_S anyway
            log.warning("apikeys.cache_invalidate_failed", error_type=type(e).__name__)


class ApiKeyAuthenticator:
    """`core.security.Authenticator` over api_keys + Redis (cache and rate limits)."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        pepper: str,
        redis: Any,
        redis_prefix: str = "aics:",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.sessions = sessions
        self.pepper = pepper
        self.redis = redis
        self.redis_prefix = redis_prefix
        self.limiter = RateLimiter(redis, prefix=redis_prefix)
        self.clock = clock

    async def authenticate(
        self, authorization: str | None, *, app_id: UUID, any_scope: tuple[str, ...]
    ) -> Principal:
        key = keys.parse_bearer(authorization)
        if key is None:
            raise UnauthenticatedError()
        prefix = keys.key_prefix(key)
        rec = await self._lookup(prefix)
        if rec is None or not keys.matches(key, rec["key_hash"], self.pepper):
            raise UnauthenticatedError()
        if rec["revoked"]:
            raise ApiKeyRevokedError()
        expires = rec["expires_at"]
        if expires is not None and datetime.fromisoformat(expires) <= self.clock():
            raise ApiKeyExpiredError()
        # A key of another app, or an app being deleted, looks like a missing app (接口规范 §2).
        if UUID(rec["app_id"]) != app_id or rec["app_status"] == "deleting":
            raise AppNotFoundError()
        if rec["app_status"] == "disabled":
            raise AppDisabledError()
        scopes = frozenset(rec["scopes"])
        if not scopes.intersection(any_scope):
            raise ForbiddenError(details={"required_scope": list(any_scope)})
        key_id = UUID(rec["id"])
        headers = await self.limiter.hit(Limits(key_id, rec["rpm_limit"], rec["daily_quota"]))
        return Principal(
            app_id=app_id, key_id=key_id, key_prefix=prefix, scopes=scopes, rate_headers=headers
        )

    async def _lookup(self, prefix: str) -> dict[str, Any] | None:
        ck = cache_key(self.redis_prefix, prefix)
        try:
            raw = await self.redis.get(ck)
        except RedisError as e:
            log.warning("apikeys.cache_unavailable", error_type=type(e).__name__)
            raw = None
        if raw:
            return json.loads(raw)
        async with self.sessions() as s:
            found = await ApiKeyRepository(s).get_by_prefix_all_apps(prefix)
        if found is None:
            return None
        row, app_status = found
        rec = {
            "id": str(row.id),
            "app_id": str(row.app_id),
            "key_hash": row.key_hash,
            "scopes": list(row.scopes),
            "rpm_limit": row.rpm_limit,
            "daily_quota": row.daily_quota,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            "revoked": row.revoked_at is not None,
            "app_status": app_status,
        }
        with contextlib.suppress(RedisError):
            await self.redis.set(ck, json.dumps(rec), ex=AUTH_CACHE_TTL_S)
        return rec
