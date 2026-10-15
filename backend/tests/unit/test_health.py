import asyncio

import pytest
from fastapi.testclient import TestClient

from aics import health
from aics.main import create_app


async def _ok() -> None:
    return None


async def _fail() -> None:
    raise ConnectionError("down")


async def _hang() -> None:
    await asyncio.sleep(10)


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_healthz(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert r.headers["x-request-id"]


def test_request_id_passthrough(client: TestClient) -> None:
    r = client.get("/healthz", headers={"X-Request-ID": "abc123"})
    assert r.headers["x-request-id"] == "abc123"


def test_readyz_all_ok(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "CHECKS", dict.fromkeys(health.CHECKS, _ok))
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json() == {
        "status": "ok",
        "checks": {"postgres": "ok", "redis": "ok", "rabbitmq": "ok", "qdrant": "ok"},
    }


def test_readyz_one_failure(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    checks = dict.fromkeys(health.CHECKS, _ok)
    checks["rabbitmq"] = _fail
    monkeypatch.setattr(health, "CHECKS", checks)
    r = client.get("/readyz")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "error"
    assert body["checks"]["rabbitmq"] == "error" and body["checks"]["postgres"] == "ok"
    assert "down" not in r.text  # 不暴露错误细节


async def test_timeout_is_error() -> None:
    res = await health.run_checks({"qdrant": _hang, "redis": _ok}, limit_s=0.05)
    assert res == {"qdrant": "error", "redis": "ok"}


def test_real_checks_fail_fast_when_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """不 mock：本机无服务时每项都应在超时内返回 error，而不是挂起。"""
    monkeypatch.setenv("AICS_DB_URL", "postgresql+asyncpg://u:p@127.0.0.1:1/x")
    monkeypatch.setenv("AICS_REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setenv("AICS_CELERY_BROKER_URL", "amqp://u:p@127.0.0.1:1/x")
    monkeypatch.setenv("AICS_QDRANT_URL", "http://127.0.0.1:1")
    from aics.core.config import get_settings

    get_settings.cache_clear()
    res = asyncio.run(health.run_checks())
    assert set(res.values()) == {"error"}


def test_qdrant_check_sends_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx
    import respx

    with respx.mock(assert_all_called=True) as mock:
        route = mock.get("http://127.0.0.1:6333/readyz").mock(return_value=httpx.Response(200))
        asyncio.run(health.check_qdrant())
        assert route.calls.last.request.headers["api-key"] == "qdrant-test"


def test_error_envelope(client: TestClient) -> None:
    r = client.get("/no-such-route")
    assert r.status_code == 404


def test_prod_hides_docs(monkeypatch: pytest.MonkeyPatch) -> None:
    from aics.core.config import get_settings

    monkeypatch.setenv("AICS_ENV", "prod")
    get_settings.cache_clear()
    c = TestClient(create_app())
    assert c.get("/docs").status_code == 404
    assert c.get("/openapi.json").status_code == 404
