"""`core.security.Authorizer` 的数据库实现（M4.1 ②）。

- `authenticate`：`Authorization: Bearer <access>` → `AuthedUser`。每次请求都要校验
  `users.token_version`（改密 / 禁用 / 刷新复用会把它 +1，旧 access 立即失效）；用户行与
  角色权限各缓存 60 秒（`{prefix}auth:who:{user_id}`、`{prefix}auth:perm:{user_id}:{app_id}`），
  授权变更时由 `AuthService` 删缓存——缓存里存的 `tv` 与 token 不一致就回表，所以踢人最多延迟
  一个 TTL，不会一直放行。
- `authorize`：`super_admin` 全放行；应用作用域路由按 `user_app_roles → roles.permissions` 判定。
  没有 `app_id` 的路由（`/users`、`/system/*` 之类）只认超级管理员。
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from aics.core.config import Settings
from aics.core.errors import ForbiddenError, UnauthenticatedError
from aics.core.security import AuthedUser
from aics.db.models.app import UserAppRole
from aics.db.models.user import Role
from aics.modules.auth import tokens
from aics.modules.auth.repository import AuthRepository

CACHE_TTL_S = 60


def _who_key(prefix: str, user_id: UUID) -> str:
    return f"{prefix}auth:who:{user_id}"


def _perm_key(prefix: str, user_id: UUID, app_id: UUID) -> str:
    return f"{prefix}auth:perm:{user_id}:{app_id}"


class AuthAuthorizer:
    def __init__(
        self,
        sessions: async_sessionmaker,
        *,
        settings: Settings,
        redis: Any | None = None,
        ttl_s: int = CACHE_TTL_S,
    ) -> None:
        self.sessions = sessions
        self.settings = settings
        self.redis = redis
        self.ttl_s = ttl_s

    # ------------------------------------------------------------ cache
    async def _cache_get(self, key: str) -> Any | None:
        if self.redis is None:
            return None
        raw = await self.redis.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return None

    async def _cache_set(self, key: str, value: Any) -> None:
        if self.redis is None:
            return
        await self.redis.set(key, json.dumps(value, ensure_ascii=False), ex=self.ttl_s)

    async def _cache_drop(self, key: str) -> None:
        if self.redis is not None:
            await self.redis.delete(key)

    # ------------------------------------------------------ authenticate
    async def authenticate(self, authorization: str | None) -> AuthedUser:
        raw = (authorization or "").strip()
        if not raw.lower().startswith("bearer "):
            raise UnauthenticatedError(details={"reason": "missing_token"})
        payload = tokens.decode(
            secret=self.settings.jwt_secret.get_secret_value(),
            token=raw.split(" ", 1)[1].strip(),
            kind=tokens.ACCESS,
        )
        key = _who_key(self.settings.redis_prefix, payload.user_id)
        cached = await self._cache_get(key)
        if isinstance(cached, dict) and cached.get("tv") == payload.token_version:
            return AuthedUser(
                id=payload.user_id,
                email=str(cached.get("email", "")),
                display_name=str(cached.get("display_name", "")),
                is_super_admin=bool(cached.get("super")),
                must_change_password=bool(cached.get("must")),
            )

        async with self.sessions() as session:
            user = await AuthRepository(session).user(payload.user_id)
        if user is None:
            raise UnauthenticatedError(details={"reason": "unknown_user"})
        if user.status != "active":
            raise UnauthenticatedError(details={"reason": "user_disabled"})
        if user.token_version != payload.token_version:
            await self._cache_drop(key)
            raise UnauthenticatedError(details={"reason": "token_revoked"})
        snapshot = {
            "tv": user.token_version,
            "email": user.email,
            "display_name": user.display_name,
            "super": user.is_super_admin,
            "must": user.must_change_password,
        }
        await self._cache_set(key, snapshot)
        return AuthedUser(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            is_super_admin=user.is_super_admin,
            must_change_password=user.must_change_password,
        )

    # --------------------------------------------------------- authorize
    async def authorize(self, user: AuthedUser, *, app_id: UUID | None, permission: str) -> None:
        if user.is_super_admin:
            return
        if app_id is None:
            raise ForbiddenError(details={"permission": permission, "reason": "super_admin_only"})
        permissions = await self.permissions_of(user.id, app_id)
        if permission not in permissions:
            raise ForbiddenError(details={"permission": permission, "app_id": str(app_id)})

    async def permissions_of(self, user_id: UUID, app_id: UUID) -> frozenset[str]:
        key = _perm_key(self.settings.redis_prefix, user_id, app_id)
        cached = await self._cache_get(key)
        if isinstance(cached, list):
            return frozenset(str(p) for p in cached)
        async with self.sessions() as session:
            result = await session.execute(
                select(Role.permissions)
                .join(UserAppRole, UserAppRole.role_id == Role.id)
                .where(UserAppRole.user_id == user_id, UserAppRole.app_id == app_id)
            )
            roles = result.scalars().all()
        permissions: set[str] = set()
        for values in roles:
            permissions.update(str(p) for p in (values or []))
        await self._cache_set(key, sorted(permissions))
        return frozenset(permissions)

    async def forget(self, user_id: UUID) -> None:
        """Drop the cached user row (called after password change / disable)."""
        await self._cache_drop(_who_key(self.settings.redis_prefix, user_id))
