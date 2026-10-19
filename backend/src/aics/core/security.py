"""Open-API principal and the scope dependency (接口规范 §2, 安全设计 §2).

The implementation (API-key lookup, rate limiting) lives in `aics.modules.apikeys`; business
modules such as chat only depend on this protocol, and `aics.main` installs the implementation
on `app.state.authenticator` (modules may not import apikeys, see import-linter contracts).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from fastapi import Request

from aics.core.errors import AppError, ForbiddenError
from aics.core.netguard import IPAddress


@dataclass(frozen=True)
class Principal:
    """An authenticated open-API caller: one API key bound to one app."""

    app_id: UUID
    key_id: UUID
    key_prefix: str
    scopes: frozenset[str]
    # Client address resolved from the TCP peer (and X-Forwarded-For behind a trusted proxy).
    client_ip: IPAddress | None = None
    # X-RateLimit-* headers to copy onto the response (streaming responses included).
    rate_headers: dict[str, str] = field(default_factory=dict)


class Authenticator(Protocol):
    async def authenticate(
        self,
        authorization: str | None,
        *,
        app_id: UUID,
        any_scope: tuple[str, ...],
        client_ip: str | None = None,
        forwarded_for: str | None = None,
    ) -> Principal:
        """Validate the bearer key for `app_id`, require one of `any_scope`, apply rate limits.

        `client_ip` is the TCP peer address and `forwarded_for` the raw `X-Forwarded-For` header;
        the implementation decides whether the header is trustworthy (可信代理) and enforces the
        key's IP allowlist.

        Raises AppError: 401 UNAUTHENTICATED / API_KEY_EXPIRED / API_KEY_REVOKED, 404 for a key
        of another app or a deleting app, 403 FORBIDDEN / APP_DISABLED / IP_NOT_ALLOWED, 429
        RATE_LIMITED / QUOTA_EXCEEDED (with Retry-After)."""
        ...

    async def authenticate_key(
        self,
        authorization: str | None,
        *,
        any_scope: tuple[str, ...],
        client_ip: str | None = None,
        forwarded_for: str | None = None,
    ) -> Principal:
        """Validate the bearer key on a path that carries no `app_id` (OpenAI 兼容 `/v1/*`).

        The application is the one the key belongs to; the rest of the chain (HMAC, revoked /
        expired / disabled, IP allowlist, scope, rate limits) is identical to `authenticate`.
        A deleting app still looks like a missing app, but there is no app to compare against.
        """
        ...


def require_scope(*any_scope: str) -> Callable[[Request, UUID], Awaitable[Principal]]:
    """FastAPI dependency for routes under `/api/v1/apps/{app_id}/...`."""

    async def dependency(request: Request, app_id: UUID) -> Principal:
        auth: Authenticator | None = getattr(request.app.state, "authenticator", None)
        if auth is None:  # pragma: no cover - wiring error; never serve unauthenticated
            raise AppError(message="鉴权未配置")
        principal = await auth.authenticate(
            request.headers.get("authorization"),
            app_id=app_id,
            any_scope=any_scope,
            client_ip=request.client.host if request.client else None,
            forwarded_for=request.headers.get("x-forwarded-for"),
        )
        request.state.principal = principal
        return principal

    return dependency


def require_key_scope(*any_scope: str) -> Callable[[Request], Awaitable[Principal]]:
    """FastAPI dependency for routes without `app_id` in the path (M3.3 `/v1/*`)."""

    async def dependency(request: Request) -> Principal:
        auth: Authenticator | None = getattr(request.app.state, "authenticator", None)
        if auth is None:  # pragma: no cover - wiring error; never serve unauthenticated
            raise AppError(message="鉴权未配置")
        principal = await auth.authenticate_key(
            request.headers.get("authorization"),
            any_scope=any_scope,
            client_ip=request.client.host if request.client else None,
            forwarded_for=request.headers.get("x-forwarded-for"),
        )
        request.state.principal = principal
        return principal

    return dependency


# --------------------------------------------------------------------------- 管理后台（M4.1）


@dataclass(frozen=True)
class AuthedUser:
    """A logged-in back-office user (JWT), independent of the open-API `Principal`."""

    id: UUID
    email: str
    display_name: str
    is_super_admin: bool = False
    must_change_password: bool = False


class Authorizer(Protocol):
    """Back-office authentication + RBAC; implemented by `modules.auth` (ADR-0017)."""

    async def authenticate(self, authorization: str | None) -> AuthedUser:
        """Decode the bearer access token and load the user.

        Raises AppError: 401 UNAUTHENTICATED (missing/malformed token, unknown user),
        401 TOKEN_EXPIRED (the front end refreshes once), 423 ACCOUNT_LOCKED."""
        ...

    async def authorize(self, user: AuthedUser, *, app_id: UUID | None, permission: str) -> None:
        """Check one dotted permission; raises 403 FORBIDDEN with `details.permission`."""
        ...


def _authorizer(request: Request) -> Authorizer:
    authorizer: Authorizer | None = getattr(request.app.state, "authorizer", None)
    if authorizer is None:  # pragma: no cover - wiring error; never serve unauthenticated
        raise AppError(message="鉴权未配置")
    return authorizer


def require_user() -> Callable[[Request], Awaitable[AuthedUser]]:
    """Dependency for `/auth/me` 与 `/auth/password`（不改密也能读自己、改密是出口）。"""

    async def dependency(request: Request) -> AuthedUser:
        user = await _authorizer(request).authenticate(request.headers.get("authorization"))
        request.state.user = user
        return user

    return dependency


def require_perm(permission: str) -> Callable[[Request], Awaitable[AuthedUser]]:
    """Dependency for every other `/api/v1/admin/*` route.

    The target app is the route's `app_id` path parameter when present; platform-level routes
    (`/users`, `/system`, `/audit`) pass `app_id=None` and rely on the permission table.
    """

    async def dependency(request: Request) -> AuthedUser:
        authorizer = _authorizer(request)
        user = await authorizer.authenticate(request.headers.get("authorization"))
        if user.must_change_password:
            raise ForbiddenError(details={"reason": "must_change_password"})
        raw_app_id = request.path_params.get("app_id")
        app_id = UUID(str(raw_app_id)) if raw_app_id else None
        await authorizer.authorize(user, app_id=app_id, permission=permission)
        request.state.user = user
        return user

    return dependency
