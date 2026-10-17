"""API key management and open-API authentication (接口规范 §2, 安全设计 §1–§2).

`ApiKeyService` creates / revokes keys (the plaintext is returned once). `ApiKeyAuthenticator`
implements `core.security.Authenticator`: bearer parsing → cached lookup by prefix
(`{prefix}apikey:{key_prefix}`, 60 s, deleted on revoke) → HMAC check → revoked / expired →
app binding and status → IP allowlist (可信代理 + X-Forwarded-For) → scope → rate limits →
usage marker (see `keyusage`).
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
from collections.abc import Callable, Iterable, Sequence
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
from aics.core.netguard import Allowlist, resolve_client_ip
from aics.core.security import Principal
from aics.db.models import ApiKey
from aics.db.models.apikey import API_KEY_SCOPES
from aics.modules.apikeys import keys, keyusage
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


class IpNotAllowedError(AppError):
    code, status, message = "IP_NOT_ALLOWED", 403, "IP 不在白名单内"


def cache_key(redis_prefix: str, key_prefix: str) -> str:
    return f"{redis_prefix}apikey:{key_prefix}"


def parse_ip_allowlist(items: Iterable[str] | None) -> list[Any] | None:
    """CIDR（或单个 IP，按 /32、/128 处理）→ `ipaddress` 网络对象；空 → None（不限制）。

    Raises ValidationAppError: 任一条目不是合法 CIDR（422，不交给 PostgreSQL 报 500）。
    """
    if not items:
        return None
    networks: list[Any] = []
    errors: list[dict[str, Any]] = []
    for raw in items:
        try:
            networks.append(ipaddress.ip_network(str(raw).strip(), strict=False))
        except ValueError:
            errors.append({"loc": ["ip_allowlist"], "msg": f"不是合法的 CIDR：{raw}"})
    if errors:
        raise ValidationAppError(details={"errors": errors})
    return networks


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
        ip_allowlist: Iterable[str] | None = None,
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
        networks = parse_ip_allowlist(ip_allowlist)
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
                ip_allowlist=networks,
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

    async def set_ip_allowlist(
        self, app_id: UUID, key_id: UUID, cidrs: Iterable[str] | None
    ) -> bool:
        """Replace a key's IP allowlist (`None`/空 = 不限制) and drop its cached record.

        Returns False when the key does not belong to the app. The cache is dropped immediately
        so a tightened allowlist takes effect on the next request (安全设计 §2)."""
        networks = parse_ip_allowlist(cidrs)
        prefix = await self.repo.set_ip_allowlist(app_id, key_id, networks)
        if prefix is None:
            return False
        await self._invalidate([prefix])
        log.info(
            "apikeys.ip_allowlist_set",
            app_id=str(app_id),
            prefix=prefix,
            cidrs=[str(n) for n in networks or []],
        )
        return True

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
        trusted_proxies: str | Iterable[str] = (),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.sessions = sessions
        self.pepper = pepper
        self.redis = redis
        self.redis_prefix = redis_prefix
        self.limiter = RateLimiter(redis, prefix=redis_prefix)
        # 只有直接对端落在这些网段内时才采信 X-Forwarded-For（core.netguard.resolve_client_ip）
        self.trusted_proxies = Allowlist.parse(
            trusted_proxies.split(",") if isinstance(trusted_proxies, str) else trusted_proxies
        )
        self.clock = clock

    async def authenticate(
        self,
        authorization: str | None,
        *,
        app_id: UUID,
        any_scope: tuple[str, ...],
        client_ip: str | None = None,
        forwarded_for: str | None = None,
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
        resolved_ip = resolve_client_ip(client_ip, forwarded_for, self.trusted_proxies)
        allowed = rec.get("ip_allowlist")
        if allowed and (
            resolved_ip is None or not Allowlist.parse(allowed).ip_allowed(resolved_ip)
        ):
            # 白名单存在时拿不到客户端地址也不放行（安全设计 §2）
            raise IpNotAllowedError(
                details={"client_ip": str(resolved_ip) if resolved_ip is not None else None}
            )
        scopes = frozenset(rec["scopes"])
        if not scopes.intersection(any_scope):
            raise ForbiddenError(details={"required_scope": list(any_scope)})
        key_id = UUID(rec["id"])
        headers = await self.limiter.hit(Limits(key_id, rec["rpm_limit"], rec["daily_quota"]))
        # last_used_at is folded in by the beat task aics.flush_key_usage (best-effort).
        await keyusage.record_use(self.redis, self.redis_prefix, key_id)
        return Principal(
            app_id=app_id,
            key_id=key_id,
            key_prefix=prefix,
            scopes=scopes,
            client_ip=resolved_ip,
            rate_headers=headers,
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
            # CIDR 列（asyncpg 读回 ipaddress 网络对象）：缓存里统一存字符串，便于 JSON 序列化
            "ip_allowlist": [str(n) for n in row.ip_allowlist] if row.ip_allowlist else None,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            "revoked": row.revoked_at is not None,
            "app_status": app_status,
        }
        with contextlib.suppress(RedisError):
            await self.redis.set(ck, json.dumps(rec), ex=AUTH_CACHE_TTL_S)
        return rec
