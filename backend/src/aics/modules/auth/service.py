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

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from aics.core.audit import REDACTED, AuditEntry, AuditSink, diff
from aics.core.config import Settings, get_settings
from aics.core.errors import (
    AccountLockedError,
    ConflictError,
    NotFoundError,
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
    MemberRow,
    SessionBody,
    TokenBody,
    UserBody,
)

log = structlog.get_logger()

MAX_FAILED_LOGINS = 5
LOCK_MINUTES = 15
LOGIN_RPM = 10
MEMBER_ROLES = ("app_admin", "viewer")


@dataclass(frozen=True)
class RequestMeta:
    ip: str | None = None
    user_agent: str | None = None
    request_id: str | None = None


_NO_META = RequestMeta()


def _user_body(user: User) -> UserBody:
    return UserBody(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_super_admin=user.is_super_admin,
        must_change_password=user.must_change_password,
        status="disabled" if user.status != "active" else "active",
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

    def _refresh_ttl(self, remember_days: int | None = None) -> timedelta:
        """refresh 有效期：没指定就用默认（1 天），指定了就用用户选的天数。"""
        days = remember_days or self.settings.jwt_refresh_ttl_days
        return timedelta(days=days)

    def _check_remember_days(self, days: int | None) -> int | None:
        """登录时用户选的「保持登录」天数：不超过配置上限，否则 422。"""
        if days is None:
            return None
        if days > self.settings.jwt_refresh_max_days:
            raise ValidationAppError(
                message=f"保持登录最多 {self.settings.jwt_refresh_max_days} 天",
                details={"remember_days_max": self.settings.jwt_refresh_max_days},
            )
        return days

    def _issue(self, user: User, kind: str, *, remember_days: int | None = None) -> tokens.Issued:
        ttl = self._access_ttl if kind == tokens.ACCESS else self._refresh_ttl(remember_days)
        return tokens.issue(
            secret=self.settings.jwt_secret.get_secret_value(),
            user_id=user.id,
            token_version=user.token_version,
            kind=kind,
            ttl=ttl,
            now=self.now(),
            remember_days=remember_days if kind == tokens.REFRESH else None,
        )

    async def _remember_refresh(self, issued: tokens.Issued) -> None:
        if self.redis is None:
            return
        # 用令牌自己的过期时间，Redis 记录与 JWT 不会错位（选了 7 天的不会被按 1 天清掉）
        ttl = max(int((issued.payload.expires_at - self.now()).total_seconds()), 1)
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

    def _pair(
        self, user: User, *, remember_days: int | None = None
    ) -> tuple[tokens.Issued, tokens.Issued]:
        return (
            self._issue(user, tokens.ACCESS),
            self._issue(user, tokens.REFRESH, remember_days=remember_days),
        )

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
    async def login(
        self,
        email: str,
        password: str,
        *,
        remember_days: int | None = None,
        meta: RequestMeta,
    ) -> SessionBody:
        days = self._check_remember_days(remember_days)
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
        access, refresh = self._pair(user, remember_days=days)
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
    async def refresh(self, refresh_token: str) -> SessionBody:
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

        # 轮换时沿用令牌里记录的保持登录天数：选了 7 天的不会因为刷新被降成 1 天
        access, refresh = self._pair(user, remember_days=payload.remember_days)
        await self._remember_refresh(refresh)
        # 与登录回同一个形状：前端刷新后直接用这里的 user / apps 恢复身份。
        # 只回 TokenBody 会让前端在 access 过期时清掉登录态（管理后台设计 §1）。
        rows = await self.repo.memberships(user.id)
        return SessionBody(
            **self._token_body(access, refresh).model_dump(),
            user=_user_body(user),
            apps=_roles(rows, super_admin=user.is_super_admin),
        )

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

    # ------------------------------------------------- 用户与应用成员（③）
    async def _forget_permissions(self, user_id: UUID, app_id: UUID) -> None:
        """Drop the cached permission set so a revocation takes effect immediately (ISSUE-72)."""
        if self.redis is None:
            return
        try:
            await self.redis.delete(f"{self.settings.redis_prefix}auth:perm:{user_id}:{app_id}")
        except RedisError as exc:  # pragma: no cover - Redis outage, cache expires anyway
            log.warning("auth.cache_drop_failed", error_type=type(exc).__name__)

    async def users(self, *, q: str | None = None, limit: int = 100) -> list[UserBody]:
        return [_user_body(user) for user in await self.repo.users(q=q, limit=limit)]

    async def create_user(
        self,
        *,
        email: str,
        display_name: str,
        password: str,
        is_super_admin: bool = False,
        actor_id: UUID | None = None,
        meta: RequestMeta = _NO_META,
    ) -> UserBody:
        normalized = email.strip().lower()
        if await self.repo.user_by_email(normalized) is not None:
            raise ConflictError(details={"email": normalized})
        user = await self.repo.create_user(
            email=normalized,
            display_name=display_name,
            password_hash=passwords.hash_password(password),
        )
        user.is_super_admin = is_super_admin
        await self.session.flush()
        await self._write_audit(
            AuditEntry(
                action="user.create",
                actor_id=actor_id,
                target_type="user",
                target_id=user.id,
                diff={
                    "email": user.email,
                    "display_name": display_name,
                    "is_super_admin": is_super_admin,
                },
                ip=meta.ip,
                user_agent=meta.user_agent,
                request_id=meta.request_id,
            )
        )
        await self.session.commit()
        return _user_body(user)

    async def update_user(
        self,
        user_id: UUID,
        *,
        display_name: str | None = None,
        status: str | None = None,
        is_super_admin: bool | None = None,
        password: str | None = None,
        actor_id: UUID | None = None,
        meta: RequestMeta = _NO_META,
    ) -> UserBody:
        user = await self.repo.user(user_id)
        if user is None:
            raise NotFoundError(details={"user_id": str(user_id)})
        before = {
            "display_name": user.display_name,
            "status": user.status,
            "is_super_admin": user.is_super_admin,
        }
        revoke = False
        if display_name is not None:
            user.display_name = display_name
        if status is not None:
            user.status = status
            revoke = revoke or status != "active"
        if is_super_admin is not None and is_super_admin != user.is_super_admin:
            user.is_super_admin = is_super_admin
            revoke = True
        if password:
            user.password_hash = passwords.hash_password(password)
            user.must_change_password = True
            revoke = True
        after = {
            "display_name": user.display_name,
            "status": user.status,
            "is_super_admin": user.is_super_admin,
        }
        if password:
            after["password"] = REDACTED
        if revoke:
            await self.repo.bump_token_version(user)
            after["token_version"] = user.token_version
        await self._write_audit(
            AuditEntry(
                action="user.update",
                actor_id=actor_id,
                target_type="user",
                target_id=user.id,
                diff=diff(before, after),
                ip=meta.ip,
                user_agent=meta.user_agent,
                request_id=meta.request_id,
            )
        )
        await self.session.commit()
        if revoke:
            await self._forget_identity(user.id)
        return _user_body(user)

    async def delete_user(
        self,
        user_id: UUID,
        *,
        actor_id: UUID | None = None,
        meta: RequestMeta = _NO_META,
    ) -> None:
        user = await self.repo.user(user_id)
        if user is None:
            raise NotFoundError(details={"user_id": str(user_id)})
        user.deleted_at = self.now()
        user.status = "disabled"
        await self.repo.bump_token_version(user)
        await self._write_audit(
            AuditEntry(
                action="user.delete",
                actor_id=actor_id,
                target_type="user",
                target_id=user.id,
                diff={"email": user.email, "deleted": True},
                ip=meta.ip,
                user_agent=meta.user_agent,
                request_id=meta.request_id,
            )
        )
        await self.session.commit()
        await self._forget_identity(user.id)

    async def reset_password(
        self,
        user_id: UUID,
        *,
        actor_id: UUID | None = None,
        meta: RequestMeta = _NO_META,
    ) -> str:
        """Generate a one-time password; the plaintext is returned once and never stored."""
        user = await self.repo.user(user_id)
        if user is None:
            raise NotFoundError(details={"user_id": str(user_id)})
        temporary = secrets.token_urlsafe(12)
        user.password_hash = passwords.hash_password(temporary)
        user.must_change_password = True
        await self.repo.bump_token_version(user)
        await self._write_audit(
            AuditEntry(
                action="user.reset_password",
                actor_id=actor_id,
                target_type="user",
                target_id=user.id,
                diff={"must_change_password": True},
                ip=meta.ip,
                user_agent=meta.user_agent,
                request_id=meta.request_id,
            )
        )
        await self.session.commit()
        await self._forget_identity(user.id)
        return temporary

    async def members(self, app_id: UUID) -> list[MemberRow]:
        rows = await self.repo.members(app_id)
        return [
            MemberRow(
                user_id=user.id,
                email=user.email,
                display_name=user.display_name,
                role_id=link.role_id,
                status="disabled" if user.status != "active" else "active",
            )
            for link, user, _role in rows
        ]

    async def set_member(
        self,
        app_id: UUID,
        user_id: UUID,
        *,
        role_id: str,
        actor_id: UUID | None = None,
        meta: RequestMeta = _NO_META,
    ) -> MemberRow:
        if role_id not in MEMBER_ROLES or await self.repo.role(role_id) is None:
            raise ValidationAppError(
                details={"errors": [{"loc": ["body", "role_id"], "msg": "未知的角色"}]}
            )
        user = await self.repo.user(user_id)
        if user is None:
            raise NotFoundError(details={"user_id": str(user_id)})
        if await self.repo.app(app_id) is None:
            raise NotFoundError(details={"app_id": str(app_id)})
        await self.repo.set_membership(user_id=user_id, app_id=app_id, role_id=role_id)
        await self._write_audit(
            AuditEntry(
                action="member.set",
                actor_id=actor_id,
                app_id=app_id,
                target_type="user",
                target_id=user_id,
                diff={"role_id": role_id},
                ip=meta.ip,
                user_agent=meta.user_agent,
                request_id=meta.request_id,
            )
        )
        await self.session.commit()
        await self._forget_identity(user_id)
        await self._forget_permissions(user_id, app_id)
        return MemberRow(
            user_id=user.id,
            email=user.email,
            display_name=user.display_name,
            role_id=role_id,
            status="disabled" if user.status != "active" else "active",
        )

    async def remove_member(
        self,
        app_id: UUID,
        user_id: UUID,
        *,
        actor_id: UUID | None = None,
        meta: RequestMeta = _NO_META,
    ) -> None:
        removed = await self.repo.delete_membership(user_id=user_id, app_id=app_id)
        if not removed:
            raise NotFoundError(details={"user_id": str(user_id), "app_id": str(app_id)})
        await self._write_audit(
            AuditEntry(
                action="member.remove",
                actor_id=actor_id,
                app_id=app_id,
                target_type="user",
                target_id=user_id,
                ip=meta.ip,
                user_agent=meta.user_agent,
                request_id=meta.request_id,
            )
        )
        await self.session.commit()
        await self._forget_identity(user_id)
        await self._forget_permissions(user_id, app_id)
