"""管理后台认证服务：登录、刷新轮换、登出、改密（管理接口参考 §认证，M4.1 ②）。

三条要点：

* **锁定**：连续 5 次密码错锁 15 分钟（`423 ACCOUNT_LOCKED` + `Retry-After`）。第 5 次失败当场就锁，
  返回 423 而不是再等一次。
* **刷新轮换**：refresh token 的 `jti` 存在 Redis（`{prefix}auth:refresh:{jti}`）。用过的 token 会被
  删掉，同一个 token 再来一次说明它泄露了——立刻 `token_version + 1` 把该用户全部会话踢掉。
  没有 Redis 时退化成「不轮换」（单测与本地都跑得起来），生产必须配 Redis。
* **审计**：登录成功 / 失败 / 登出 / 改密都写 `audit_logs`（`core.audit` 端口，M4.1 ③ 才提供
  `GET /audit`）。`diff` 只记结果与计数，绝不带密码。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from aics.core.audit import AuditEntry, AuditSink
from aics.core.config import Settings, get_settings
from aics.core.errors import (
    AccountLockedError,
    UnauthenticatedError,
    ValidationAppError,
)
from aics.core.metrics import inc
from aics.core.ratelimit import FixedWindowLimiter, Limits
from aics.db.models.app import App, UserAppRole
from aics.db.models.user import Role, User
from aics.modules.auth import passwords, tokens
from aics.modules.auth.permissions import ALL_PERMISSIONS
from aics.modules.auth.repository import AuthRepository
from aics.modules.auth.schemas import (
    AppMembership,
    MeBody,
    SessionBody,
    TokenBody,
    UserBody,
)

log = structlog.get_logger()

MAX_FAILED_LOGINS = 5
LOCK_MINUTES = 15
LOGIN_RPM = 10


@dataclass(frozen=True)
class RequestMeta:
    ip: str | None = None
    user_agent: str | None = None
    request_id: str | None = None


def _user_body(user: User) -> UserBody:
    return UserBody(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_super_admin=user.is_super_admin,
        must_change_password=user.must_change_password,
    )


def _roles(rows: list[tuple[UserAppRole, App, Role]], *, super_admin: bool) -> list[AppMembership]:
    out: list[AppMembership] = []
    for link, app, role in rows:
        perms = list(ALL_PERMISSIONS) if super_admin else sorted(role.permissions or [])
        out.append(
            AppMembership(app_id=app.id, app_name=app.name, role_id=link.role_id, permissions=perms)
        )
    return out


class AuthService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        redis: Any | None = None,
        audit: AuditSink | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.redis = redis
        self.audit = audit
        self.now = now or (lambda: datetime.now(UTC))
        self.repo = AuthRepository(session)
        self.limiter = (
            FixedWindowLimiter(redis, prefix=self.settings.redis_prefix) if redis else None
        )

    # ---------------------------------------------------------------- tokens
    @property
    def _access_ttl(self) -> timedelta:
        return timedelta(minutes=self.settings.jwt_access_ttl_min)

    @property
    def _refresh_ttl(self) -> timedelta:
        return timedelta(days=self.settings.jwt_refresh_ttl_days)

    def _issue(self, user: User, kind: str) -> tokens.Issued:
        ttl = self._access_ttl if kind == tokens.ACCESS else self._refresh_ttl
        return tokens.issue(
            secret=self.settings.jwt_secret.get_secret_value(),
            user_id=user.id,
            token_version=user.token_version,
            kind=kind,
            ttl=ttl,
            now=self.now(),
        )

    async def _remember_refresh(self, issued: tokens.Issued) -> None:
        if self.redis is None:
            return
        ttl = int(self._refresh_ttl.total_seconds())
        await self.redis.set(
            self._refresh_key(issued.payload.jti), str(issued.payload.user_id), ex=ttl
        )

    def _refresh_key(self, jti: str) -> str:
        return f"{self.settings.redis_prefix}auth:refresh:{jti}"

    async def _forget_refresh(self, jti: str) -> None:
        if self.redis is not None:
            await self.redis.delete(self._refresh_key(jti))

    async def _forget_identity(self, user_id: UUID) -> None:
        """Drop the authorizer's 60 s identity cache after a token_version bump.

        The cache stores the version it validated, so without this a revoked or
        rotated access token would keep working for up to `ttl_s` seconds.
        """
        if self.redis is None:
            return
        try:
            await self.redis.delete(f"{self.settings.redis_prefix}auth:who:{user_id}")
        except RedisError as exc:  # pragma: no cover - Redis outage, cache expires anyway
            log.warning("auth.cache_drop_failed", error_type=type(exc).__name__)

    def _pair(self, user: User) -> tuple[tokens.Issued, tokens.Issued]:
        return self._issue(user, tokens.ACCESS), self._issue(user, tokens.REFRESH)

    def _token_body(self, access: tokens.Issued, refresh: tokens.Issued) -> TokenBody:
        return TokenBody(
            access_token=access.token,
            refresh_token=refresh.token,
            expires_in=int(self._access_ttl.total_seconds()),
        )

    # ----------------------------------------------------------------- audit
    async def _write_audit(self, entry: AuditEntry) -> None:
        if self.audit is None:
            return
        await self.audit.write(self.session, entry)

    # ----------------------------------------------------------------- login
    async def login(self, email: str, password: str, *, meta: RequestMeta) -> SessionBody:
        if self.limiter is not None:
            await self.limiter.hit(
                Limits(key=f"login:{meta.ip or 'unknown'}", limit=LOGIN_RPM, window_s=60)
            )
        user = await self.repo.user_by_email(email)
        now = self.now()
        if user is None:
            # Same message and same code as a wrong password: never reveal whether the email exists.
            raise UnauthenticatedError(message="邮箱或密码不正确")
        if user.status != "active":
            raise UnauthenticatedError(message="邮箱或密码不正确")
        if user.locked_until is not None and user.locked_until > now:
            raise self._locked(user.locked_until, now)

        if not passwords.verify_password(password, user.password_hash):
            count = await self.repo.record_failure(user, at=now, lock_minutes=LOCK_MINUTES)
            await self._write_audit(
                AuditEntry(
                    action="auth.login",
                    actor_id=user.id,
                    target_type="user",
                    target_id=str(user.id),
                    diff={"result": "failed", "failed_logins": count},
                    ip=meta.ip,
                    user_agent=meta.user_agent,
                    request_id=meta.request_id,
                )
            )
            await self.session.commit()
            inc("aics_login_failed_total")
            if user.locked_until is not None and user.locked_until > now:
                raise self._locked(user.locked_until, now)
            raise UnauthenticatedError(message="邮箱或密码不正确")

        user.failed_logins = 0
        user.locked_until = None
        user.last_login_at = now
        if passwords.needs_rehash(user.password_hash):
            user.password_hash = passwords.hash_password(password)
        access, refresh = self._pair(user)
        await self._remember_refresh(refresh)
        rows = await self.repo.memberships(user.id)
        body = SessionBody(
            **self._token_body(access, refresh).model_dump(),
            user=_user_body(user),
            apps=_roles(rows, super_admin=user.is_super_admin),
        )
        await self._write_audit(
            AuditEntry(
                action="auth.login",
                actor_id=user.id,
                target_type="user",
                target_id=str(user.id),
                diff={"result": "ok"},
                ip=meta.ip,
                user_agent=meta.user_agent,
                request_id=meta.request_id,
            )
        )
        await self.session.commit()
        inc("aics_login_total")
        return body

    def _locked(self, until: datetime, now: datetime) -> AccountLockedError:
        seconds = max(int((until - now).total_seconds()), 1)
        return AccountLockedError(
            details={"retry_after_seconds": seconds},
            headers={"Retry-After": str(seconds)},
        )

    # --------------------------------------------------------------- refresh
    async def refresh(self, refresh_token: str) -> TokenBody:
        payload = tokens.decode(
            secret=self.settings.jwt_secret.get_secret_value(),
            token=refresh_token,
            kind=tokens.REFRESH,
        )
        user = await self.repo.user(payload.user_id)
        if user is None or user.status != "active":
            raise UnauthenticatedError(details={"reason": "unknown_user"})
        if user.token_version != payload.token_version:
            raise UnauthenticatedError(details={"reason": "token_revoked"})

        if self.redis is not None:
            key = self._refresh_key(payload.jti)
            if not await self.redis.get(key):
                # The token was already rotated (or logged out). Treat as reuse: kill everything.
                await self.repo.bump_token_version(user)
                await self._forget_identity(user.id)
                await self._write_audit(
                    AuditEntry(
                        action="auth.refresh_reused",
                        actor_id=user.id,
                        target_type="user",
                        target_id=str(user.id),
                    )
                )
                await self.session.commit()
                inc("aics_refresh_reused_total")
                raise UnauthenticatedError(details={"reason": "refresh_reused"})
            await self.redis.delete(key)

        access, refresh = self._pair(user)
        await self._remember_refresh(refresh)
        return self._token_body(access, refresh)

    # ---------------------------------------------------------------- logout
    async def logout(self, refresh_token: str | None) -> None:
        if not refresh_token:
            return
        try:
            payload = tokens.decode(
                secret=self.settings.jwt_secret.get_secret_value(),
                token=refresh_token,
                kind=tokens.REFRESH,
            )
        except Exception:  # noqa: BLE001 - logout must never fail on a stale token
            return
        await self._forget_refresh(payload.jti)

    # -------------------------------------------------------------------- me
    async def me(self, user_id: UUID) -> MeBody:
        user = await self.repo.user(user_id)
        if user is None:
            raise UnauthenticatedError(details={"reason": "unknown_user"})
        rows = await self.repo.memberships(user.id)
        return MeBody(user=_user_body(user), apps=_roles(rows, super_admin=user.is_super_admin))

    # ------------------------------------------------------- change password
    async def change_password(
        self, user_id: UUID, *, current: str, new: str, meta: RequestMeta
    ) -> TokenBody:
        user = await self.repo.user(user_id)
        if user is None:
            raise UnauthenticatedError(details={"reason": "unknown_user"})
        if not passwords.verify_password(current, user.password_hash):
            raise ValidationAppError(
                details={"errors": [{"loc": ["body", "current_password"], "msg": "当前密码不正确"}]}
            )
        if current == new:
            raise ValidationAppError(
                details={
                    "errors": [{"loc": ["body", "new_password"], "msg": "新密码不能与当前密码相同"}]
                }
            )
        user.password_hash = passwords.hash_password(new)
        user.must_change_password = False
        await self.repo.bump_token_version(user)
        await self._forget_identity(user.id)
        access, refresh = self._pair(user)
        await self._remember_refresh(refresh)
        await self._write_audit(
            AuditEntry(
                action="auth.password_change",
                actor_id=user.id,
                target_type="user",
                target_id=str(user.id),
                diff={"password": "***", "token_version": user.token_version},
                ip=meta.ip,
                user_agent=meta.user_agent,
                request_id=meta.request_id,
            )
        )
        await self.session.commit()
        return self._token_body(access, refresh)
