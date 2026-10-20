"""M4.5 ④：Webhook 管理接口（`/api/v1/admin/apps/{app_id}/webhook*`）的服务层与协议层。

服务层用假 session / 假 repo 验证明文、审计与 404 语义；协议层只证明路由把参数原样交给
service、`response_model` 生效、401 / 403 / 404 / 422 的形状正确。真实的投递与重试在集成
用例（`test_webhook_admin_pg.py`）里覆盖。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from aics.core.config import get_settings
from aics.core.errors import ForbiddenError, NotFoundError, UnauthenticatedError
from aics.core.security import AuthedUser
from aics.main import create_app
from aics.modules.webhooks.admin_service import WebhookAdminService, webhook_body
from aics.modules.webhooks.service import DeliveryResult
from aics.modules.webhooks.service import TestResult as WebhookTestResult
from aics.rag.config import WebhookConfig

APP_ID = uuid4()
USER_ID = uuid4()
DELIVERY_ID = uuid4()


def delivery(**over: Any) -> Any:
    now = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
    row = SimpleNamespace(
        id=DELIVERY_ID,
        event="message.created",
        seq=7,
        conversation_id=None,
        status="failed",
        attempts=6,
        last_status_code=500,
        last_error="HTTP 500",
        next_attempt_at=None,
        delivered_at=None,
        created_at=now,
    )
    for key, value in over.items():
        setattr(row, key, value)
    return row


class FakeAppRow:
    def __init__(self, webhook: dict[str, Any] | None = None) -> None:
        self.webhook_config = webhook or {}
        self.deleted_at = None
        self.status = "active"


class FakeSession:
    def __init__(self, app: FakeAppRow | None) -> None:
        self.app = app
        self.commits = 0

    async def execute(self, _stmt: Any) -> Any:
        return SimpleNamespace(scalar_one_or_none=lambda: self.app)

    async def commit(self) -> None:
        self.commits += 1


class FakeRepo:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.calls: list[tuple[Any, ...]] = []
        self.reset: bool | None = True

    async def list_recent(self, app_id: UUID, **kwargs: Any) -> list[Any]:
        self.calls.append(("list_recent", app_id, kwargs))
        return self.rows

    async def get(self, app_id: UUID, id: UUID) -> Any:
        self.calls.append(("get", app_id, id))
        return self.rows[0] if self.rows else None

    async def reset_for_redelivery(self, app_id: UUID, id: UUID, *, at: datetime) -> int:
        self.calls.append(("reset", app_id, id))
        if self.rows and self.reset:
            self.rows[0] = delivery(status="pending")
        return 1 if self.reset else 0


class FakeDeliverer:
    def __init__(self, repo: FakeRepo) -> None:
        self.repo = repo
        self.redelivered: list[tuple[UUID, UUID]] = []
        self.tested: list[UUID] = []

    async def redeliver(self, app_id: UUID, delivery_id: UUID) -> bool:
        self.redelivered.append((app_id, delivery_id))
        changed = await self.repo.reset_for_redelivery(app_id, delivery_id, at=datetime.now(UTC))
        return bool(changed)

    async def send_test(self, app_id: UUID) -> WebhookTestResult:
        self.tested.append(app_id)
        return WebhookTestResult(ok=True, status_code=204, elapsed_ms=12)


class FakeSink:
    def __init__(self) -> None:
        self.entries: list[Any] = []

    async def write(self, _session: Any, entry: Any) -> None:
        self.entries.append(entry)


def service(
    *, app: FakeAppRow | None = None, rows: list[Any] | None = None, sink: FakeSink | None = None
) -> Any:
    session = FakeSession(app if app is not None else FakeAppRow())
    svc = WebhookAdminService(
        session, settings=get_settings(), audit=sink if sink is not None else FakeSink()
    )
    repo = FakeRepo(rows if rows is not None else [delivery()])
    svc.repo = repo  # type: ignore[assignment]
    svc.deliverer = FakeDeliverer(repo)  # type: ignore[assignment]
    return svc


def test_webhook_body_hides_ciphertext() -> None:
    cfg = WebhookConfig(
        url="https://example.com/hook", secret_enc="enc", secret_prefix="whsec_abcd"
    )
    assert webhook_body(cfg) == {
        "url": "https://example.com/hook",
        "secret_prefix": "whsec_abcd",
        "ready": True,
    }
    assert webhook_body(WebhookConfig()) == {"url": None, "secret_prefix": None, "ready": False}


async def test_deliveries_pass_filters_and_hide_payload() -> None:
    svc = service()
    rows = await svc.deliveries(APP_ID, status="failed", limit=500, before=None)
    assert svc.repo.calls[0] == (
        "list_recent",
        APP_ID,
        {"status": "failed", "limit": 100, "before": None},
    )
    assert rows[0]["id"] == str(DELIVERY_ID)
    assert "payload" not in rows[0]
    assert rows[0]["status"] == "failed"


async def test_deliveries_of_missing_app_is_404() -> None:
    svc = service(app=None)
    svc.session.app = None  # type: ignore[union-attr]
    with pytest.raises(Exception) as caught:
        await svc.deliveries(APP_ID, status=None, limit=10, before=None)
    assert getattr(caught.value, "code", None) == "APP_NOT_FOUND"


async def test_redeliver_resets_and_audits() -> None:
    sink = FakeSink()
    svc = service(sink=sink)
    row = await svc.redeliver(APP_ID, DELIVERY_ID, actor_id=USER_ID)
    assert svc.deliverer.redelivered == [(APP_ID, DELIVERY_ID)]
    assert row["status"] == "pending"
    assert sink.entries[0].action == "webhook.redeliver"
    assert sink.entries[0].target_id == DELIVERY_ID
    assert sink.entries[0].diff == {"before": {"status": "failed"}, "after": {"status": "pending"}}


async def test_redeliver_unknown_is_404() -> None:
    svc = service(rows=[])
    with pytest.raises(NotFoundError) as caught:
        await svc.redeliver(APP_ID, DELIVERY_ID, actor_id=USER_ID)
    assert caught.value.details["delivery_id"] == str(DELIVERY_ID)


async def test_redeliver_pending_row_is_rejected() -> None:
    svc = service()
    svc.repo.rows[0].status = "pending"  # type: ignore[union-attr]
    with pytest.raises(NotFoundError) as caught:
        await svc.redeliver(APP_ID, DELIVERY_ID, actor_id=USER_ID)
    assert caught.value.details["delivery_id"] == str(DELIVERY_ID)


async def test_redeliver_when_reset_changes_nothing_keeps_row() -> None:
    """行没被改（例如并发已复位）时仍返回当前行，不报 404。"""

    svc = service()
    svc.repo.reset = False  # type: ignore[union-attr]
    svc.repo.rows[0].status = "delivered"  # type: ignore[union-attr]
    row = await svc.redeliver(APP_ID, DELIVERY_ID, actor_id=USER_ID)
    assert row["status"] == "delivered"


async def test_send_test_and_audit() -> None:
    sink = FakeSink()
    svc = service(sink=sink)
    body = await svc.test(APP_ID, actor_id=USER_ID)
    assert body == {"ok": True, "status_code": 204, "error": None, "elapsed_ms": 12}
    assert sink.entries[0].action == "webhook.test"
    assert sink.entries[0].diff == {"ok": True, "status_code": 204}


async def test_rotate_secret_returns_plaintext_once_and_stores_ciphertext() -> None:
    sink = FakeSink()
    app = FakeAppRow({"url": "https://example.com/hook"})
    svc = service(app=app, sink=sink)
    body = await svc.rotate_secret(APP_ID, actor_id=USER_ID)
    assert body["secret"].startswith("whsec_")
    assert body["secret_prefix"] == body["secret"][: len(body["secret_prefix"])]
    assert app.webhook_config["secret_prefix"] == body["secret_prefix"]
    assert app.webhook_config["secret_enc"] != body["secret"]
    assert app.webhook_config["url"] == "https://example.com/hook"
    assert sink.entries[0].action == "webhook.secret"


async def test_no_actor_means_no_audit() -> None:
    sink = FakeSink()
    svc = service(sink=sink)
    await svc.redeliver(APP_ID, DELIVERY_ID, actor_id=None)
    assert sink.entries == []


class FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def config(self, app_id: UUID) -> dict[str, Any]:
        self.calls.append(("config", app_id))
        return webhook_body(WebhookConfig())

    async def deliveries(self, app_id: UUID, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("deliveries", app_id, kwargs))
        return [dict(delivery_row(delivery())) for _ in range(1)]

    async def redeliver(self, app_id: UUID, delivery_id: UUID, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("redeliver", app_id, delivery_id, kwargs))
        return delivery_row(delivery(status="pending"))

    async def test(self, app_id: UUID, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("test", app_id, kwargs))
        return WebhookTestResult(ok=True, status_code=204, elapsed_ms=5).as_dict()

    async def rotate_secret(self, app_id: UUID, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("rotate", app_id, kwargs))
        return {"secret": "whsec_x", "secret_prefix": "whsec_x"}


def delivery_row(row: Any) -> dict[str, Any]:
    from aics.modules.webhooks.admin_service import delivery_row as real

    return real(row)


@asynccontextmanager
async def _session() -> AsyncIterator[Any]:
    yield object()


class FakeRuntime:
    def __init__(self, service: Any) -> None:
        self._service = service

    def session(self) -> Any:
        return _session()

    def service(self, _session: Any) -> Any:
        return self._service


class FakeAuthorizer:
    def __init__(self, *, deny: str | None = None) -> None:
        self.deny = deny

    async def authenticate(self, authorization: str | None) -> AuthedUser:
        if authorization != "Bearer good-token":
            raise UnauthenticatedError(details={"reason": "missing_token"})
        return AuthedUser(
            id=USER_ID, email="admin@example.com", display_name="管理员", is_super_admin=False
        )

    async def authorize(self, user: Any, *, app_id: Any, permission: str) -> None:
        if self.deny is not None and permission == self.deny:
            raise ForbiddenError(details={"reason": "missing_permission", "permission": permission})


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, FakeService]:
    fake = FakeService()
    app = create_app()
    app.state.webhooks_admin_runtime = FakeRuntime(fake)
    app.state.authorizer = FakeAuthorizer()
    return TestClient(app), fake


@pytest.fixture
def headers() -> dict[str, str]:
    return {"authorization": "Bearer good-token"}


BASE = f"/api/v1/admin/apps/{APP_ID}"


def test_get_webhook_ok(rig: tuple[TestClient, FakeService], headers: dict[str, str]) -> None:
    http, fake = rig
    response = http.get(f"{BASE}/webhook", headers=headers)
    assert response.status_code == 200
    assert response.json()["ready"] is False
    assert fake.calls[0] == ("config", APP_ID)


def test_list_deliveries_passes_filters(
    rig: tuple[TestClient, FakeService], headers: dict[str, str]
) -> None:
    http, fake = rig
    response = http.get(
        f"{BASE}/webhook-deliveries", headers=headers, params={"status": "failed", "limit": 5}
    )
    assert response.status_code == 200
    assert response.json()["items"][0]["id"] == str(DELIVERY_ID)
    assert fake.calls[0] == (
        "deliveries",
        APP_ID,
        {"status": "failed", "limit": 5, "before": None},
    )


def test_list_deliveries_rejects_bad_status(
    rig: tuple[TestClient, FakeService], headers: dict[str, str]
) -> None:
    http, _ = rig
    response = http.get(f"{BASE}/webhook-deliveries", headers=headers, params={"status": "nope"})
    assert response.status_code == 422


def test_redeliver_ok(rig: tuple[TestClient, FakeService], headers: dict[str, str]) -> None:
    http, fake = rig
    response = http.post(f"{BASE}/webhook-deliveries/{DELIVERY_ID}/redeliver", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "pending"
    call = fake.calls[0]
    assert call[0] == "redeliver" and call[1] == APP_ID and call[2] == DELIVERY_ID
    assert call[3]["actor_id"] == USER_ID


def test_send_test_ok(rig: tuple[TestClient, FakeService], headers: dict[str, str]) -> None:
    http, fake = rig
    response = http.post(f"{BASE}/webhook/test", headers=headers)
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert fake.calls[0][0] == "test"


def test_rotate_secret_ok(rig: tuple[TestClient, FakeService], headers: dict[str, str]) -> None:
    http, fake = rig
    response = http.post(f"{BASE}/webhook/secret", headers=headers)
    assert response.status_code == 200
    assert response.json()["secret"] == "whsec_x"
    assert fake.calls[0][0] == "rotate"


def test_missing_token_is_401(rig: tuple[TestClient, FakeService]) -> None:
    http, _ = rig
    response = http.get(f"{BASE}/webhook")
    assert response.status_code == 401


def test_forbidden_is_403(monkeypatch: pytest.MonkeyPatch, headers: dict[str, str]) -> None:
    fake = FakeService()
    app = create_app()
    app.state.webhooks_admin_runtime = FakeRuntime(fake)
    app.state.authorizer = FakeAuthorizer(deny="webhook.write")
    http = TestClient(app)
    response = http.post(f"{BASE}/webhook/test", headers=headers)
    assert response.status_code == 403


def test_redeliver_404_mapping(monkeypatch: pytest.MonkeyPatch, headers: dict[str, str]) -> None:
    class NotFoundService(FakeService):
        async def redeliver(self, app_id: UUID, delivery_id: UUID, **kwargs: Any) -> Any:
            raise NotFoundError(details={"delivery_id": str(delivery_id)})

    app = create_app()
    app.state.webhooks_admin_runtime = FakeRuntime(NotFoundService())
    app.state.authorizer = FakeAuthorizer()
    http = TestClient(app)
    response = http.post(f"{BASE}/webhook-deliveries/{DELIVERY_ID}/redeliver", headers=headers)
    assert response.status_code == 404
    assert response.json()["error"]["details"]["delivery_id"] == str(DELIVERY_ID)


def test_delivery_row_serialises_datetimes() -> None:
    from aics.modules.webhooks.admin_service import delivery_row as real

    body = real(delivery())
    assert body["created_at"].startswith("2026-10-20T10:00")
    assert body["conversation_id"] is None


def test_delivery_result_as_dict() -> None:
    assert DeliveryResult(status="delivered", attempts=1, status_code=200).as_dict()["status"] == (
        "delivered"
    )
