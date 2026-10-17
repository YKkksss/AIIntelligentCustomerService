"""M2.6 ① API key format, rate limiter and authenticator decisions (接口规范 §2 / §6).

M3.1 ② 追加：IP 白名单（可信代理 + X-Forwarded-For 解析、白名单命中 / 拒绝、缓存失效）。
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from aics.core.errors import AppError
from aics.modules.apikeys import keys
from aics.modules.apikeys.ratelimit import Limits, RateLimiter
from aics.modules.apikeys.service import ApiKeyAuthenticator, cache_key
from tests.fake_redis import FakeRedis

PEPPER = "p" * 40
APP = uuid.uuid4()
KEY_ID = uuid.uuid4()


def test_key_format_and_hash() -> None:
    k = keys.generate_key()
    assert keys.is_well_formed(k) and k.startswith("aics_") and len(k) == 37
    assert keys.key_prefix(k) == k[:12]
    h = keys.hash_key(k, PEPPER)
    assert len(h) == 64 and keys.matches(k, h, PEPPER)
    assert not keys.matches(k, h, "q" * 40)  # pepper matters
    assert keys.generate_key() != k


@pytest.mark.parametrize(
    "header,ok",
    [
        (None, False),
        ("", False),
        ("Basic abc", False),
        ("Bearer aics_short", False),
        ("Bearer sk-" + "a" * 32, False),
        ("Bearer aics_" + "a" * 31 + "!", False),
        ("Bearer aics_" + "A1" * 16, True),
        ("bearer   aics_" + "A1" * 16 + "  ", True),
    ],
)
def test_parse_bearer(header: str | None, ok: bool) -> None:
    assert (keys.parse_bearer(header) is not None) is ok


async def test_rate_limit_minute_window() -> None:
    r = FakeRedis()
    rl = RateLimiter(r)
    lim = Limits(KEY_ID, rpm_limit=2, daily_quota=None)
    t = 1_800_000_010.0  # 10 s into a minute
    h1 = await rl.hit(lim, now=t)
    assert h1["X-RateLimit-Limit"] == "2" and h1["X-RateLimit-Remaining"] == "1"
    assert int(h1["X-RateLimit-Reset"]) % 60 == 0
    await rl.hit(lim, now=t + 1)
    with pytest.raises(AppError) as ei:
        await rl.hit(lim, now=t + 2)
    e = ei.value
    assert e.status == 429 and e.code == "RATE_LIMITED"
    assert e.headers["Retry-After"] == "48" and e.headers["X-RateLimit-Remaining"] == "0"
    # next minute: a fresh window
    assert (await rl.hit(lim, now=t + 60))["X-RateLimit-Remaining"] == "1"
    assert all(ttl == 120 for k, ttl in r.ttl.items() if ":rl:" in k)


async def test_daily_quota() -> None:
    rl = RateLimiter(FakeRedis())
    lim = Limits(KEY_ID, rpm_limit=100, daily_quota=2)
    t = 1_800_000_000.0
    await rl.hit(lim, now=t)
    await rl.hit(lim, now=t + 1)
    with pytest.raises(AppError) as ei:
        await rl.hit(lim, now=t + 2)
    assert ei.value.code == "QUOTA_EXCEEDED" and int(ei.value.headers["Retry-After"]) > 0


async def test_rate_limit_fails_open_when_redis_is_down() -> None:
    rl = RateLimiter(FakeRedis(fail=True))
    assert await rl.hit(Limits(KEY_ID, 1, 1)) == {}


def _rec(**kw: Any) -> dict[str, Any]:
    rec = {
        "id": str(KEY_ID),
        "app_id": str(APP),
        "key_hash": "",
        "scopes": ["chat"],
        "rpm_limit": 60,
        "daily_quota": None,
        "expires_at": None,
        "revoked": False,
        "app_status": "active",
    }
    rec.update(kw)
    return rec


def _auth(
    rec: dict[str, Any] | None, key: str, *, trusted: str = ""
) -> tuple[ApiKeyAuthenticator, FakeRedis]:
    r = FakeRedis()
    if rec is not None:
        rec["key_hash"] = rec["key_hash"] or keys.hash_key(key, PEPPER)
        r.data[cache_key("aics:", keys.key_prefix(key))] = json.dumps(rec)

    class NoDb:
        def __call__(self) -> Any:
            raise AssertionError("cache hit expected")

    auth = ApiKeyAuthenticator(NoDb(), pepper=PEPPER, redis=r, trusted_proxies=trusted)  # type: ignore[arg-type]
    return auth, r


async def test_authenticate_success_from_cache() -> None:
    k = keys.generate_key()
    auth, _ = _auth(_rec(), k)
    p = await auth.authenticate(f"Bearer {k}", app_id=APP, any_scope=("chat",))
    assert p.app_id == APP and p.key_id == KEY_ID and p.scopes == {"chat"}
    assert p.rate_headers["X-RateLimit-Limit"] == "60"


@pytest.mark.parametrize(
    "rec_kw,app,scope,code,status",
    [
        ({"key_hash": "0" * 64}, APP, "chat", "UNAUTHENTICATED", 401),
        ({"revoked": True}, APP, "chat", "API_KEY_REVOKED", 401),
        (
            {"expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()},
            APP,
            "chat",
            "API_KEY_EXPIRED",
            401,
        ),
        ({}, uuid.uuid4(), "chat", "APP_NOT_FOUND", 404),
        ({"app_status": "deleting"}, APP, "chat", "APP_NOT_FOUND", 404),
        ({"app_status": "disabled"}, APP, "chat", "APP_DISABLED", 403),
        ({}, APP, "kb:write", "FORBIDDEN", 403),
    ],
)
async def test_authenticate_rejections(
    rec_kw: dict[str, Any], app: uuid.UUID, scope: str, code: str, status: int
) -> None:
    k = keys.generate_key()
    auth, _ = _auth(_rec(**rec_kw), k)
    with pytest.raises(AppError) as ei:
        await auth.authenticate(f"Bearer {k}", app_id=app, any_scope=(scope,))
    assert (ei.value.code, ei.value.status) == (code, status)


async def test_authenticate_future_expiry_and_any_scope() -> None:
    k = keys.generate_key()
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    auth, _ = _auth(_rec(expires_at=future, scopes=["handoff:write"]), k)
    p = await auth.authenticate(f"Bearer {k}", app_id=APP, any_scope=("chat", "handoff:write"))
    assert p.scopes == {"handoff:write"}


async def test_authenticate_missing_header() -> None:
    auth, _ = _auth(None, keys.generate_key())
    with pytest.raises(AppError) as ei:
        await auth.authenticate(None, app_id=APP, any_scope=("chat",))
    assert ei.value.code == "UNAUTHENTICATED"


@pytest.mark.parametrize(
    "kw,field",
    [
        ({"name": " ", "scopes": ["chat"]}, "name"),
        ({"name": "k", "scopes": []}, "scopes"),
        ({"name": "k", "scopes": ["admin"]}, "scopes"),
        ({"name": "k", "scopes": ["chat"], "rpm_limit": 0}, "rpm_limit"),
        ({"name": "k", "scopes": ["chat"], "daily_quota": 0}, "daily_quota"),
        ({"name": "k", "scopes": ["chat"], "ip_allowlist": ["10.0.0.0/33"]}, "ip_allowlist"),
        ({"name": "k", "scopes": ["chat"], "ip_allowlist": ["办公室"]}, "ip_allowlist"),
    ],
)
async def test_create_validation(kw: dict[str, Any], field: str) -> None:
    from aics.modules.apikeys.service import ApiKeyService

    svc = ApiKeyService(None, pepper=PEPPER)  # type: ignore[arg-type]
    with pytest.raises(AppError) as ei:
        await svc.create(APP, **kw)
    assert ei.value.code == "VALIDATION_ERROR"
    assert [e["loc"] for e in ei.value.details["errors"]] == [[field]]


@pytest.mark.parametrize(
    "items,expected",
    [
        (None, None),
        ([], None),
        (["10.0.0.0/8"], ["10.0.0.0/8"]),
        ([" 192.168.1.5 "], ["192.168.1.5/32"]),  # 单个 IP 按 /32
        (["2001:db8::1"], ["2001:db8::1/128"]),
        (["10.1.2.3/8"], ["10.0.0.0/8"]),  # 按掩码归一化（strict=False）
        (["10.0.0.0/8", "2001:db8::/32"], ["10.0.0.0/8", "2001:db8::/32"]),
    ],
)
def test_parse_ip_allowlist_normalizes(items: list[str] | None, expected: list[str] | None) -> None:
    from aics.modules.apikeys.service import parse_ip_allowlist

    nets = parse_ip_allowlist(items)
    assert (None if nets is None else [str(n) for n in nets]) == expected


async def test_ip_allowlist_allows_listed_address() -> None:
    k = keys.generate_key()
    auth, _ = _auth(_rec(ip_allowlist=["10.0.0.0/8", "2001:db8::/32"]), k)

    p = await auth.authenticate(
        f"Bearer {k}", app_id=APP, any_scope=("chat",), client_ip="10.9.8.7"
    )
    assert str(p.client_ip) == "10.9.8.7"
    p6 = await auth.authenticate(
        f"Bearer {k}", app_id=APP, any_scope=("chat",), client_ip="2001:db8::9"
    )
    assert str(p6.client_ip) == "2001:db8::9"


@pytest.mark.parametrize(
    ("client_ip", "xff", "trusted"),
    [
        ("203.0.113.9", None, ""),  # 不在白名单
        (None, None, ""),  # 拿不到地址 → 不放行
        ("203.0.113.9", "10.1.2.3", ""),  # 直连时伪造 XFF 无效
        ("198.51.100.7", "10.1.2.3", "172.17.0.0/16"),  # 对端不可信 → 仍以对端为准
        ("172.17.0.5", "1.2.3.4, 203.0.113.9", "172.17.0.0/16"),  # 可信代理但客户端不在白名单
        ("172.17.0.5", "203.0.113.9, garbage", "172.17.0.0/16"),  # 链非法 → 不判定
    ],
)
async def test_ip_allowlist_rejections(
    client_ip: str | None, xff: str | None, trusted: str
) -> None:
    k = keys.generate_key()
    auth, _ = _auth(_rec(ip_allowlist=["10.0.0.0/8"]), k, trusted=trusted)

    with pytest.raises(AppError) as ei:
        await auth.authenticate(
            f"Bearer {k}",
            app_id=APP,
            any_scope=("chat",),
            client_ip=client_ip,
            forwarded_for=xff,
        )

    assert (ei.value.code, ei.value.status) == ("IP_NOT_ALLOWED", 403)
    assert "client_ip" in ei.value.details


async def test_ip_allowlist_behind_trusted_proxy_uses_forwarded_client() -> None:
    k = keys.generate_key()
    auth, _ = _auth(_rec(ip_allowlist=["10.0.0.0/8"]), k, trusted="172.17.0.0/16, 172.18.0.0/16")

    p = await auth.authenticate(
        f"Bearer {k}",
        app_id=APP,
        any_scope=("chat",),
        client_ip="172.17.0.5",
        forwarded_for="10.1.2.3, 172.18.0.9",
    )

    assert str(p.client_ip) == "10.1.2.3"


async def test_key_without_allowlist_accepts_any_address() -> None:
    k = keys.generate_key()
    auth, _ = _auth(_rec(), k)

    p = await auth.authenticate(
        f"Bearer {k}", app_id=APP, any_scope=("chat",), client_ip="203.0.113.9"
    )

    assert str(p.client_ip) == "203.0.113.9"


async def test_set_ip_allowlist_validates_and_invalidates_cache() -> None:
    from aics.modules.apikeys.service import ApiKeyService

    row_prefix = "aics_AAAAAA"
    seen: list[Any] = []

    class Repo:
        def __init__(self, s: Any) -> None:
            pass

        async def set_ip_allowlist(self, app_id: Any, key_id: Any, networks: Any) -> str | None:
            seen.append(networks)
            return row_prefix if key_id == KEY_ID else None

    import aics.modules.apikeys.service as svc_mod

    orig = svc_mod.ApiKeyRepository
    svc_mod.ApiKeyRepository = Repo  # type: ignore[misc]
    try:
        r = FakeRedis()
        r.data[cache_key("aics:", row_prefix)] = "{}"
        svc = ApiKeyService(None, pepper=PEPPER, redis=r)  # type: ignore[arg-type]

        assert await svc.set_ip_allowlist(APP, KEY_ID, ["10.0.0.0/8"])
        assert [str(n) for n in seen[0]] == ["10.0.0.0/8"]
        assert cache_key("aics:", row_prefix) not in r.data  # 立即失效，改严即刻生效
        assert await svc.set_ip_allowlist(APP, KEY_ID, None)  # 清空 = 不限制
        assert seen[1] is None
        assert not await svc.set_ip_allowlist(APP, uuid.uuid4(), ["10.0.0.0/8"])  # 不属于该应用
        with pytest.raises(AppError) as ei:
            await svc.set_ip_allowlist(APP, KEY_ID, ["nope"])
        assert ei.value.code == "VALIDATION_ERROR"
    finally:
        svc_mod.ApiKeyRepository = orig  # type: ignore[misc]


async def test_lookup_falls_back_to_db_and_caches() -> None:
    from types import SimpleNamespace

    from aics.modules.apikeys import service as svc_mod

    k = keys.generate_key()
    row = SimpleNamespace(
        id=KEY_ID,
        app_id=APP,
        key_hash=keys.hash_key(k, PEPPER),
        scopes=["chat"],
        rpm_limit=5,
        daily_quota=None,
        ip_allowlist=None,
        expires_at=None,
        revoked_at=None,
    )

    class Repo:
        def __init__(self, s: Any) -> None:
            pass

        async def get_by_prefix_all_apps(self, prefix: str) -> Any:
            return (row, "active") if prefix == k[:12] else None

    class Sessions:
        def __call__(self) -> Any:
            return self

        async def __aenter__(self) -> Any:
            return None

        async def __aexit__(self, *a: object) -> None:
            return None

    orig = svc_mod.ApiKeyRepository
    svc_mod.ApiKeyRepository = Repo  # type: ignore[misc]
    try:
        r = FakeRedis()
        auth = ApiKeyAuthenticator(Sessions(), pepper=PEPPER, redis=r)  # type: ignore[arg-type]
        p = await auth.authenticate(f"Bearer {k}", app_id=APP, any_scope=("chat",))
        assert p.key_id == KEY_ID and r.ttl[cache_key("aics:", k[:12])] == 60
        with pytest.raises(AppError):
            await auth.authenticate("Bearer aics_" + "Q" * 32, app_id=APP, any_scope=("chat",))
        down = ApiKeyAuthenticator(Sessions(), pepper=PEPPER, redis=FakeRedis(fail=True))  # type: ignore[arg-type]
        assert (await down.authenticate(f"Bearer {k}", app_id=APP, any_scope=("chat",))).key_id
    finally:
        svc_mod.ApiKeyRepository = orig  # type: ignore[misc]
