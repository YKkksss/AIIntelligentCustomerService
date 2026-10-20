"""M4.5 ③：反馈运营接口（`/api/v1/admin/apps/{app_id}/feedback*`）的协议层。

只证明路由把筛选与请求体原样交给 service、`response_model` 生效、viewer 打码，以及
401 / 403 / 404 / 422 的形状；`handled` 的真实写库与审计在集成用例里覆盖。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from aics.core.errors import (
    ForbiddenError,
    NotFoundError,
    UnauthenticatedError,
    ValidationAppError,
)
from aics.core.security import AuthedUser
from aics.main import create_app
from aics.modules.feedback import admin_router
from aics.modules.feedback.admin_service import decode_cursor

APP_ID = uuid4()
FEEDBACK_ID = uuid4()
MESSAGE_ID = uuid4()
CONVERSATION_ID = uuid4()
USER_ID = uuid4()


def feedback_body(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": str(FEEDBACK_ID),
        "message_id": str(MESSAGE_ID),
        "conversation_id": str(CONVERSATION_ID),
        "rating": -1,
        "comment": "答非所问",
        "source": "api",
        "handled": False,
        "created_at": "2026-10-20T10:00:00+00:00",
        "updated_at": "2026-10-20T10:00:00+00:00",
        "message": {"role": "assistant", "intent": "faq", "excerpt": "很抱歉……"},
    }
    body.update(over)
    return body


class FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.error: Exception | None = None

    def _maybe_raise(self) -> None:
        if self.error is not None:
            raise self.error

    async def list(self, app_id: UUID, **kwargs: Any) -> Any:
        self.calls.append(("list", app_id, kwargs))
        self._maybe_raise()
        return {"items": [feedback_body()], "next_cursor": "next"}

    async def handle(self, app_id: UUID, feedback_id: UUID, **kwargs: Any) -> Any:
        self.calls.append(("handle", app_id, feedback_id, kwargs))
        self._maybe_raise()
        return feedback_body(handled=True)


@asynccontextmanager
async def _session() -> AsyncIterator[Any]:
    yield object()


class FakeRuntime:
    def sessions(self) -> Any:
        return _session()


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
    service = FakeService()
    app = create_app()
    app.state.feedback_runtime = FakeRuntime()
    # viewer：没有 app.write → 评论与摘要打码
    app.state.authorizer = FakeAuthorizer(deny="app.write")
    monkeypatch.setattr(admin_router, "FeedbackAdminService", lambda *a, **k: service)
    return TestClient(app), service


@pytest.fixture
def headers() -> dict[str, str]:
    return {"authorization": "Bearer good-token"}


BASE = f"/api/v1/admin/apps/{APP_ID}/feedback"


def test_cursor_garbage_is_a_422() -> None:
    with pytest.raises(ValidationAppError) as caught:
        decode_cursor("nope")
    assert caught.value.details["errors"][0]["loc"] == ["cursor"]


def test_list_passes_filters_and_masks(
    rig: tuple[TestClient, FakeService], headers: dict[str, str]
) -> None:
    http, service = rig
    response = http.get(
        f"{BASE}",
        headers=headers,
        params={"rating": -1, "handled": "false", "limit": 5, "cursor": "abc"},
    )
    assert response.status_code == 200
    assert response.json()["next_cursor"] == "next"
    call = service.calls[0]
    assert call[0] == "list"
    assert call[2]["rating"] == -1
    assert call[2]["handled"] is False
    assert call[2]["limit"] == 5
    assert call[2]["cursor"] == "abc"
    assert call[2]["mask"] is True


def test_list_is_unmasked_for_an_app_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeService()
    app = create_app()
    app.state.feedback_runtime = FakeRuntime()
    app.state.authorizer = FakeAuthorizer()
    monkeypatch.setattr(admin_router, "FeedbackAdminService", lambda *a, **k: service)
    TestClient(app).get(BASE, headers={"authorization": "Bearer good-token"})
    assert service.calls[0][2]["mask"] is False


def test_handle_passes_handled_and_actor(
    rig: tuple[TestClient, FakeService], headers: dict[str, str]
) -> None:
    http, service = rig
    response = http.patch(f"{BASE}/{FEEDBACK_ID}", headers=headers, json={"handled": True})
    assert response.status_code == 200
    assert response.json()["handled"] is True
    call = service.calls[0]
    assert call[0] == "handle"
    assert call[2] == FEEDBACK_ID
    assert call[3]["handled"] is True
    assert call[3]["actor_id"] == USER_ID


def test_handle_defaults_to_true(
    rig: tuple[TestClient, FakeService], headers: dict[str, str]
) -> None:
    http, service = rig
    assert http.patch(f"{BASE}/{FEEDBACK_ID}", headers=headers, json={}).status_code == 200
    assert service.calls[0][3]["handled"] is True


def test_handle_404_is_mapped(rig: tuple[TestClient, FakeService], headers: dict[str, str]) -> None:
    http, service = rig
    service.error = NotFoundError(
        message="反馈不存在或不属于本应用", details={"feedback_id": str(FEEDBACK_ID)}
    )
    response = http.patch(f"{BASE}/{FEEDBACK_ID}", headers=headers, json={"handled": True})
    assert response.status_code == 404
    assert response.json()["error"]["details"]["feedback_id"] == str(FEEDBACK_ID)


def test_handle_rejects_unknown_fields(
    rig: tuple[TestClient, FakeService], headers: dict[str, str]
) -> None:
    http, _ = rig
    response = http.patch(f"{BASE}/{FEEDBACK_ID}", headers=headers, json={"handled": True, "x": 1})
    assert response.status_code == 422


def test_handle_rejects_a_non_boolean(
    rig: tuple[TestClient, FakeService], headers: dict[str, str]
) -> None:
    http, _ = rig
    response = http.patch(f"{BASE}/{FEEDBACK_ID}", headers=headers, json={"handled": "maybe"})
    assert response.status_code == 422


def test_missing_token_is_401(rig: tuple[TestClient, FakeService]) -> None:
    http, _ = rig
    assert http.get(BASE).status_code == 401


def test_log_read_permission_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app()
    app.state.feedback_runtime = FakeRuntime()
    app.state.authorizer = FakeAuthorizer(deny="log.read")
    monkeypatch.setattr(admin_router, "FeedbackAdminService", lambda *a, **k: FakeService())
    response = TestClient(app).get(BASE, headers={"authorization": "Bearer good-token"})
    assert response.status_code == 403


def test_handle_requires_feedback_write(monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app()
    app.state.feedback_runtime = FakeRuntime()
    app.state.authorizer = FakeAuthorizer(deny="feedback.write")
    monkeypatch.setattr(admin_router, "FeedbackAdminService", lambda *a, **k: FakeService())
    response = TestClient(app).patch(
        f"{BASE}/{FEEDBACK_ID}", headers={"authorization": "Bearer good-token"}, json={}
    )
    assert response.status_code == 403


def test_limit_above_max_is_rejected(
    rig: tuple[TestClient, FakeService], headers: dict[str, str]
) -> None:
    http, _ = rig
    assert http.get(BASE, headers=headers, params={"limit": 500}).status_code == 422
