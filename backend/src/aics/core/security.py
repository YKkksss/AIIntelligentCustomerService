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

from aics.core.errors import AppError


@dataclass(frozen=True)
class Principal:
    """An authenticated open-API caller: one API key bound to one app."""

    app_id: UUID
    key_id: UUID
    key_prefix: str
    scopes: frozenset[str]
    # X-RateLimit-* headers to copy onto the response (streaming responses included).
    rate_headers: dict[str, str] = field(default_factory=dict)


class Authenticator(Protocol):
    async def authenticate(
        self, authorization: str | None, *, app_id: UUID, any_scope: tuple[str, ...]
    ) -> Principal:
        """Validate the bearer key for `app_id`, require one of `any_scope`, apply rate limits.

        Raises AppError: 401 UNAUTHENTICATED / API_KEY_EXPIRED / API_KEY_REVOKED, 404 for a key
        of another app or a deleting app, 403 FORBIDDEN / APP_DISABLED, 429 RATE_LIMITED /
        QUOTA_EXCEEDED (with Retry-After)."""
        ...


def require_scope(*any_scope: str) -> Callable[[Request, UUID], Awaitable[Principal]]:
    """FastAPI dependency for routes under `/api/v1/apps/{app_id}/...`."""

    async def dependency(request: Request, app_id: UUID) -> Principal:
        auth: Authenticator | None = getattr(request.app.state, "authenticator", None)
        if auth is None:  # pragma: no cover - wiring error; never serve unauthenticated
            raise AppError(message="鉴权未配置")
        principal = await auth.authenticate(
            request.headers.get("authorization"), app_id=app_id, any_scope=any_scope
        )
        request.state.principal = principal
        return principal

    return dependency
