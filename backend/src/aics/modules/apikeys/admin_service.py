"""Key 管理接口的业务层（M4.5 ②）。

创建、吊销与缓存失效都复用开放接口的 `ApiKeyService`；这一层只做三件事：

1. **脱敏视图**——列表与详情永不返回明文与 `key_hash`，只回 `prefix`、`scopes`、限额与时间戳
   （安全设计 §1：明文只在创建那次响应里出现）；
2. **审计同事务**——`key.create` / `key.update` / `key.revoke` 与写操作一起提交，`diff` 走
   `core.audit.diff`（`key_hash`、`ip_allowlist` 之外的敏感键会被自动打码）；
3. **接入示例**——`GET /keys/{id}/example` 返回 curl / Python / JavaScript 三段，Key 用占位符。

`apikeys` 是独立模块（import-linter）：审计端口从 `core.audit` 取，由 `aics.main` 注入。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import structlog

from aics.core.audit import AuditEntry, AuditSink, diff
from aics.core.errors import NotFoundError
from aics.db.models import ApiKey
from aics.modules.apikeys.repository import ApiKeyRepository
from aics.modules.apikeys.service import DEFAULT_RPM_LIMIT, ApiKeyService

log = structlog.get_logger(__name__)

#: 列表里展示前缀的末若干位，方便与日志里的 `prefix` 对照（不是密钥本体）。
HINT_TAIL = 4
KEY_TARGET = "api_key"
KEEP: Any = object()


@dataclass(frozen=True)
class ActorMeta:
    """审计要记的请求侧信息（IP / UA / request_id）；路由层从 `request` 里取。"""

    ip: str | None = None
    user_agent: str | None = None
    request_id: str | None = None


_NO_META = ActorMeta()


def _jsonable(value: Any) -> Any:
    """UUID / datetime → 字符串：审计 `diff` 直接写 jsonb，不能带这些类型。"""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def key_row(row: ApiKey, *, now: datetime | None = None) -> dict[str, Any]:
    """One key in the shape the management API exposes (never the secret)."""
    moment = now or datetime.now(UTC)
    if row.revoked_at is not None:
        status = "revoked"
    elif row.expires_at is not None and row.expires_at <= moment:
        status = "expired"
    else:
        status = "active"
    return {
        "id": row.id,
        "name": row.name,
        "prefix": row.prefix,
        "hint": f"{row.prefix}…{row.prefix[-HINT_TAIL:]}",
        "scopes": list(row.scopes or []),
        "rpm_limit": row.rpm_limit,
        "daily_quota": row.daily_quota,
        "ip_allowlist": [str(n) for n in (row.ip_allowlist or [])],
        "last_used_at": row.last_used_at,
        "expires_at": row.expires_at,
        "revoked_at": row.revoked_at,
        "created_at": row.created_at,
        "status": status,
    }


def _snippets(*, base_url: str, app_id: UUID) -> dict[str, str]:
    url = f"{base_url}/api/v1/apps/{app_id}"
    return {
        "curl": (
            f'curl -s -H "Authorization: Bearer <YOUR_API_KEY>" \\\n  "{url}/documents?limit=1"'
        ),
        "python": (
            "import httpx\n\n"
            "client = httpx.Client(\n"
            f'    base_url="{base_url}",\n'
            '    headers={"Authorization": "Bearer <YOUR_API_KEY>"},\n'
            ")\n"
            f'print(client.get("/api/v1/apps/{app_id}/documents").json())'
        ),
        "javascript": (
            f'const res = await fetch("{url}/documents?limit=1", {{\n'
            '  headers: { Authorization: "Bearer <YOUR_API_KEY>" },\n'
            "});\n"
            "console.log(await res.json());"
        ),
        "url": url,
    }


class ApiKeyAdminService:
    def __init__(
        self,
        session: Any,
        *,
        pepper: str,
        redis: Any = None,
        redis_prefix: str = "aics:",
        audit: AuditSink | None = None,
    ) -> None:
        self.session = session
        self.repo = ApiKeyRepository(session)
        self.audit = audit
        self.keys = ApiKeyService(session, pepper=pepper, redis=redis, redis_prefix=redis_prefix)

    async def list_keys(self, app_id: UUID) -> list[dict[str, Any]]:
        rows = await self.repo.list_for_app(app_id)
        now = datetime.now(UTC)
        return [key_row(r, now=now) for r in rows]

    async def create_key(
        self,
        app_id: UUID,
        *,
        name: str,
        scopes: Sequence[str],
        rpm_limit: int = DEFAULT_RPM_LIMIT,
        daily_quota: int | None = None,
        expires_at: datetime | None = None,
        ip_allowlist: Iterable[str] | None = None,
        actor_id: UUID | None = None,
        meta: ActorMeta = _NO_META,
    ) -> dict[str, Any]:
        """Create a key and return it **with the plaintext** — the only time it is visible."""
        row, plain = await self.keys.create(
            app_id,
            name=name,
            scopes=scopes,
            rpm_limit=rpm_limit,
            daily_quota=daily_quota,
            expires_at=expires_at,
            ip_allowlist=ip_allowlist,
            created_by=actor_id,
        )
        body = key_row(row)
        await self._audit(
            "key.create",
            app_id,
            actor_id=actor_id,
            target_id=row.id,
            payload={"after": {k: v for k, v in body.items() if k != "hint"}},
            meta=meta,
        )
        await self.session.commit()
        log.info("apikeys.admin_created", app_id=str(app_id), prefix=row.prefix)
        return {**body, "key": plain}

    async def update_key(
        self,
        app_id: UUID,
        key_id: UUID,
        *,
        rpm_limit: int | None = None,
        daily_quota: int | None | Any = KEEP,
        ip_allowlist: Iterable[str] | None | Any = KEEP,
        actor_id: UUID | None = None,
        meta: ActorMeta = _NO_META,
    ) -> dict[str, Any]:
        row = await self._get(app_id, key_id)
        before = key_row(row)
        if rpm_limit is not None or daily_quota is not KEEP:
            changed = await self.keys.update_limits(
                app_id,
                key_id,
                rpm_limit=row.rpm_limit if rpm_limit is None else rpm_limit,
                daily_quota=row.daily_quota if daily_quota is KEEP else daily_quota,
            )
            if not changed:
                raise self._missing(key_id)
        if ip_allowlist is not KEEP:
            allow = None if ip_allowlist is None else ip_allowlist
            if not await self.keys.set_ip_allowlist(app_id, key_id, allow):
                raise self._missing(key_id)
        await self.session.refresh(row)
        after = key_row(row)
        changed = diff(before, after)
        if changed["before"] or changed["after"]:
            await self._audit(
                "key.update",
                app_id,
                actor_id=actor_id,
                target_id=key_id,
                payload=changed,
                meta=meta,
            )
            await self.session.commit()
        return after

    async def revoke_key(
        self,
        app_id: UUID,
        key_id: UUID,
        *,
        actor_id: UUID | None = None,
        meta: ActorMeta = _NO_META,
    ) -> dict[str, Any]:
        if not await self.keys.revoke(app_id, key_id):
            raise self._missing(key_id)
        row = await self.repo.get(app_id, key_id)
        if row is None:  # pragma: no cover - the revoke above touched the same row
            raise self._missing(key_id)
        await self._audit(
            "key.revoke",
            app_id,
            actor_id=actor_id,
            target_id=key_id,
            payload={"after": {"revoked_at": row.revoked_at}},
            meta=meta,
        )
        await self.session.commit()
        return key_row(row)

    async def example(self, app_id: UUID, key_id: UUID, *, base_url: str) -> dict[str, Any]:
        """README-style snippets; the secret itself is never taken back out of the database."""
        row = await self._get(app_id, key_id)
        return {"key_id": row.id, "name": row.name, **_snippets(base_url=base_url, app_id=app_id)}

    async def _get(self, app_id: UUID, key_id: UUID) -> ApiKey:
        row = await self.repo.get(app_id, key_id)
        if row is None:
            raise self._missing(key_id)
        return row

    @staticmethod
    def _missing(key_id: UUID) -> NotFoundError:
        return NotFoundError(
            message="API Key 不存在或不属于本应用", details={"key_id": str(key_id)}
        )

    async def _audit(
        self,
        action: str,
        app_id: UUID,
        *,
        actor_id: UUID | None,
        target_id: UUID | None,
        payload: dict[str, Any] | None = None,
        meta: ActorMeta,
    ) -> None:
        if self.audit is None or actor_id is None:
            return
        await self.audit.write(
            self.session,
            AuditEntry(
                action=action,
                actor_id=actor_id,
                app_id=app_id,
                target_type=KEY_TARGET,
                target_id=target_id,
                diff=cast("dict[str, Any] | None", _jsonable(payload)),
                ip=meta.ip,
                user_agent=meta.user_agent,
                request_id=meta.request_id,
            ),
        )
