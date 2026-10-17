"""M2.6 ① (integration): API keys in PG + real Redis behind a FastAPI route.

Covers: wrong key 401, revoked / expired codes, scope 403, A-app key on B app 404, revoke
invalidates the 60 s cache immediately, rpm limit 429 with Retry-After, X-RateLimit headers.

M3.1 ② 追加：IP 白名单（真实 TCP 对端地址 + 可信代理转发头、改严立即生效、清空即放行）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

import httpx
import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from aics.core.errors import AppError, error_body
from aics.core.security import Principal, require_scope
from aics.modules.apikeys.service import ApiKeyAuthenticator, ApiKeyService

pytestmark = pytest.mark.integration
PEPPER = "x" * 40


def make_app(pg: dict[str, Any], redis: Any, *, trusted: str = "") -> FastAPI:
    app = FastAPI()
    app.state.authenticator = ApiKeyAuthenticator(
        pg["sessions"],
        pepper=PEPPER,
        redis=redis,
        redis_prefix="aics_it:",
        trusted_proxies=trusted,
    )

    @app.exception_handler(AppError)
    async def _err(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            error_body(exc.code, exc.message, None, exc.details),
            status_code=exc.status,
            headers=exc.headers or None,
        )

    @app.get("/api/v1/apps/{app_id}/ping")
    async def ping(
        app_id: UUID, p: Annotated[Principal, Depends(require_scope("chat", "handoff:write"))]
    ) -> JSONResponse:
        return JSONResponse({"app": str(p.app_id)}, headers=p.rate_headers)

    return app


async def create_key(pg: dict[str, Any], redis: Any, app_id: UUID, **kw: Any) -> tuple[UUID, str]:
    async with pg["sessions"]() as s:
        svc = ApiKeyService(s, pepper=PEPPER, redis=redis, redis_prefix="aics_it:")
        row, plain = await svc.create(app_id, name="it", scopes=kw.pop("scopes", ["chat"]), **kw)
        await s.commit()
        return row.id, plain


async def test_auth_paths(pg: dict[str, Any], redis_it: Any) -> None:
    a, b = pg["app_a"], pg["app_b"]
    key_id, key = await create_key(pg, redis_it, a)
    _, kb_key = await create_key(pg, redis_it, a, scopes=["kb:read"])
    _, expired = await create_key(
        pg, redis_it, a, expires_at=datetime.now(UTC) - timedelta(minutes=1)
    )
    transport = httpx.ASGITransport(app=make_app(pg, redis_it))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:

        async def call(app_id: UUID, k: str | None) -> httpx.Response:
            h = {"Authorization": f"Bearer {k}"} if k else {}
            return await c.get(f"/api/v1/apps/{app_id}/ping", headers=h)

        r = await call(a, key)
        assert r.status_code == 200 and r.json() == {"app": str(a)}
        assert r.headers["X-RateLimit-Limit"] == "60"
        assert (await call(a, None)).json()["error"]["code"] == "UNAUTHENTICATED"
        wrong = key[:12] + ("A" if key[12] != "A" else "B") + key[13:]
        assert (await call(a, wrong)).status_code == 401
        assert (await call(a, "aics_" + "Z" * 32)).status_code == 401  # unknown prefix
        assert (await call(b, key)).json()["error"]["code"] == "APP_NOT_FOUND"  # isolation
        assert (await call(a, kb_key)).json()["error"]["code"] == "FORBIDDEN"
        assert (await call(a, expired)).json()["error"]["code"] == "API_KEY_EXPIRED"
        # revoke → the cached record is dropped, next call fails immediately
        async with pg["sessions"]() as s:
            svc = ApiKeyService(s, pepper=PEPPER, redis=redis_it, redis_prefix="aics_it:")
            assert await svc.revoke(a, key_id)
            assert not await svc.revoke(a, key_id)  # idempotent
            assert not await svc.revoke(b, key_id)  # app-scoped
            await s.commit()
        assert (await call(a, key)).json()["error"]["code"] == "API_KEY_REVOKED"


async def test_rate_limit_429(pg: dict[str, Any], redis_it: Any) -> None:
    import asyncio
    import time

    _, key = await create_key(pg, redis_it, pg["app_a"], rpm_limit=1)
    transport = httpx.ASGITransport(app=make_app(pg, redis_it))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        h = {"Authorization": f"Bearer {key}"}
        url = f"/api/v1/apps/{pg['app_a']}/ping"
        if time.time() % 60 > 58:  # keep both calls inside one minute window
            await asyncio.sleep(2.5)
        first = await c.get(url, headers=h)
        r = await c.get(url, headers=h)
    assert first.status_code == 200 and first.headers["X-RateLimit-Remaining"] == "0"
    assert r.status_code == 429 and r.json()["error"]["code"] == "RATE_LIMITED"
    assert 1 <= int(r.headers["Retry-After"]) <= 60 and r.headers["X-RateLimit-Remaining"] == "0"


async def test_app_deleting_and_disabled(pg: dict[str, Any], redis_it: Any) -> None:
    from sqlalchemy import update

    from aics.db.models import App

    a = pg["app_a"]
    _, key = await create_key(pg, redis_it, a)
    transport = httpx.ASGITransport(app=make_app(pg, redis_it))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        h = {"Authorization": f"Bearer {key}"}
        for status, code in (("disabled", "APP_DISABLED"), ("deleting", "APP_NOT_FOUND")):
            async with pg["sessions"]() as s:
                await s.execute(update(App).where(App.id == a).values(status=status))
                await s.commit()
            await redis_it.delete(f"aics_it:apikey:{key[:12]}")  # skip the 60 s cache
            r = await c.get(f"/api/v1/apps/{a}/ping", headers=h)
            assert r.json()["error"]["code"] == code


async def call_from(
    app: FastAPI, url: str, key: str, *, client: tuple[str, int], xff: str | None = None
) -> httpx.Response:
    """One request with a chosen TCP peer address (and optional X-Forwarded-For)."""
    headers = {"Authorization": f"Bearer {key}"}
    if xff is not None:
        headers["X-Forwarded-For"] = xff
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=client), base_url="http://t"
    ) as c:
        return await c.get(url, headers=headers)


async def test_ip_allowlist_enforced_and_cleared(pg: dict[str, Any], redis_it: Any) -> None:
    a = pg["app_a"]
    key_id, key = await create_key(pg, redis_it, a, ip_allowlist=["10.0.0.0/8"])
    app = make_app(pg, redis_it)
    url = f"/api/v1/apps/{a}/ping"

    allowed = await call_from(app, url, key, client=("10.1.2.3", 5000))
    assert allowed.status_code == 200
    denied = await call_from(app, url, key, client=("203.0.113.9", 5000))
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "IP_NOT_ALLOWED"
    assert denied.json()["error"]["details"]["client_ip"] == "203.0.113.9"

    async with pg["sessions"]() as s:
        svc = ApiKeyService(s, pepper=PEPPER, redis=redis_it, redis_prefix="aics_it:")
        assert await svc.set_ip_allowlist(a, key_id, None)  # 清空 = 不限制
        await s.commit()

    assert (await call_from(app, url, key, client=("203.0.113.9", 5000))).status_code == 200


async def test_ip_allowlist_uses_forwarded_client_behind_trusted_proxy(
    pg: dict[str, Any], redis_it: Any
) -> None:
    a = pg["app_a"]
    _, key = await create_key(pg, redis_it, a, ip_allowlist=["10.0.0.0/8"])
    trusted = make_app(pg, redis_it, trusted="172.17.0.0/16")
    untrusted = make_app(pg, redis_it)  # 未配置可信代理
    url = f"/api/v1/apps/{a}/ping"

    # 对端是可信代理：真实客户端取自 X-Forwarded-For
    ok = await call_from(trusted, url, key, client=("172.17.0.5", 5000), xff="10.1.2.3")
    assert ok.status_code == 200
    bad = await call_from(trusted, url, key, client=("172.17.0.5", 5000), xff="203.0.113.9")
    assert bad.json()["error"]["code"] == "IP_NOT_ALLOWED"
    # 同一个请求头，直连时会被忽略：以对端地址为准
    spoofed = await call_from(untrusted, url, key, client=("203.0.113.9", 5000), xff="10.1.2.3")
    assert spoofed.json()["error"]["code"] == "IP_NOT_ALLOWED"
