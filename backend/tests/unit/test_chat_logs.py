"""M4.5 ③：会话日志与待回访的协议层（`/api/v1/admin/apps/{app_id}/conversations*`、`/callbacks*`）。

业务规则在集成用例里覆盖（真 PG 的筛选、游标与状态迁移）；这里只证明：路由把查询参数与
请求体原样交给 service、`response_model` 生效、viewer 打码、以及 401 / 403 / 404 / 409 / 422
的错误体形状。游标的编解码是纯函数，单独覆盖。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from aics.core.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UnauthenticatedError,
)
from aics.core.security import AuthedUser
from aics.main import create_app
from aics.modules.chat import admin_router
from aics.modules.chat.logs import DEFAULT_CHANNELS, decode_cursor, encode_cursor

APP_ID = uuid4()
CONVERSATION_ID = uuid4()
USER_ID = uuid4()


def conv_body(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": str(CONVERSATION_ID),
        "channel": "api",
        "status": "active",
        "title": None,
        "external_user_id": "u-1",
        "created_at": "2026-10-20T10:00:00+00:00",
        "last_activity_at": "2026-10-20T10:05:00+00:00",
        "closed_at": None,
        "close_reason": None,
        "message_count": 4,
        "handoff_status": "none",
        "handoff_reason": None,
        "handoff_requested_at": None,
        "handoff_end_reason": None,
        "handoff_agent_name": None,
        "needs_callback": False,
        "callback_reason": None,
        "callback_requested_at": None,
        "callback_cleared_at": None,
        "callback_note": None,
        "intent": "faq",
        "downvotes": 0,
    }
    body.update(over)
    return body


class FakeLogs:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.error: Exception | None = None
        self.mask = False

    def _maybe_raise(self) -> None:
        if self.error is not None:
            raise self.error

    async def list(self, app_id: UUID, *, filters: Any, cursor: str | None, limit: int) -> Any:
        self.calls.append(("list", app_id, filters, cursor, limit))
        self._maybe_raise()
        return {"items": [conv_body()], "next_cursor": "abc"}

    async def detail(self, app_id: UUID, conversation_id: UUID, *, mask: bool) -> Any:
        self.calls.append(("detail", app_id, conversation_id, mask))
        self._maybe_raise()
        return {
            "conversation": conv_body(),
            "messages": [
                {
                    "id": str(uuid4()),
                    "role": "assistant",
                    "kind": None,
                    "agent_id": None,
                    "content": "13800001111",
                    "created_at": "2026-10-20T10:01:00+00:00",
                    "answer_type": "answer",
                    "intent": "faq",
                    "handoff": False,
                    "degraded": False,
                    "citations": [],
                }
            ],
            "summary": {
                "value": {"topics": ["退款"]},
                "version": 2,
                "until_msg_id": None,
                "updated_at": None,
            },
            "facts": [
                {
                    "key": "order_id",
                    "value": "A-1",
                    "category": "order",
                    "confidence": 0.9,
                    "version": 1,
                    "first_seen_at": None,
                }
            ],
        }

    async def force_release(
        self,
        app_id: UUID,
        conversation_id: UUID,
        *,
        actor_id: UUID | None,
        note: str | None,
        meta: dict[str, Any] | None,
    ) -> Any:
        self.calls.append(("force_release", app_id, conversation_id, actor_id, note))
        self._maybe_raise()
        return conv_body(handoff_status="released", handoff_end_reason="admin")

    async def callbacks(
        self, app_id: UUID, *, cursor: str | None, limit: int, include_cleared: bool
    ) -> Any:
        self.calls.append(("callbacks", app_id, cursor, limit, include_cleared))
        self._maybe_raise()
        return {"items": [conv_body(needs_callback=True)], "next_cursor": None}

    async def clear_callback(
        self,
        app_id: UUID,
        conversation_id: UUID,
        *,
        note: str | None,
        actor_id: UUID | None,
        meta: dict[str, Any] | None,
    ) -> Any:
        self.calls.append(("clear_callback", app_id, conversation_id, note, actor_id))
        self._maybe_raise()
        return conv_body(needs_callback=False, callback_cleared_at="2026-10-20T11:00:00+00:00")


@asynccontextmanager
async def _session() -> AsyncIterator[Any]:
    yield object()


class FakeRuntime:
    redis = None
    redis_prefix = "aics:"

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
def rig(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, FakeLogs]:
    logs = FakeLogs()
    app = create_app()
    app.state.chat_runtime = FakeRuntime()
    # viewer：没有 app.write → 详情打码
    app.state.authorizer = FakeAuthorizer(deny="app.write")
    monkeypatch.setattr(admin_router, "ConversationLogs", lambda *a, **k: logs)
    return TestClient(app), logs


@pytest.fixture
def headers() -> dict[str, str]:
    return {"authorization": "Bearer good-token"}


BASE = f"/api/v1/admin/apps/{APP_ID}"


def test_cursor_roundtrip_keeps_microseconds() -> None:
    at = datetime(2026, 10, 20, 10, 0, 0, 123456, tzinfo=UTC)
    raw = encode_cursor(at, CONVERSATION_ID)
    assert raw is not None
    assert decode_cursor(raw) == (at, CONVERSATION_ID)
    assert decode_cursor(None) is None


def test_cursor_garbage_is_a_422_with_a_field_path() -> None:
    from aics.core.errors import ValidationAppError

    with pytest.raises(ValidationAppError) as caught:
        decode_cursor("not-a-cursor")
    assert caught.value.details["errors"][0]["loc"] == ["cursor"]


def test_list_passes_default_channels_and_filters(rig: tuple[TestClient, FakeLogs]) -> None:
    http, logs = rig
    response = http.get(
        f"{BASE}/conversations",
        headers={"authorization": "Bearer good-token"},
        params={"status": "closed", "intent": "faq,order", "downvoted": "true", "q": " 退款 "},
    )
    assert response.status_code == 200
    assert response.json()["next_cursor"] == "abc"
    call = logs.calls[0]
    assert call[0] == "list"
    filters = call[2]
    assert filters.channels == DEFAULT_CHANNELS
    assert filters.status == "closed"
    assert filters.intents == ("faq", "order")
    assert filters.downvoted is True
    assert filters.q == "退款"


def test_list_accepts_explicit_channel_and_limit(rig: tuple[TestClient, FakeLogs]) -> None:
    http, logs = rig
    response = http.get(
        f"{BASE}/conversations",
        headers={"authorization": "Bearer good-token"},
        params={"channel": "debug,eval", "limit": 5, "cursor": "abc"},
    )
    assert response.status_code == 200
    filters = logs.calls[0][2]
    assert filters.channels == ("debug", "eval")
    assert logs.calls[0][3] == "abc"
    assert logs.calls[0][4] == 5


def test_limit_above_max_is_rejected(rig: tuple[TestClient, FakeLogs]) -> None:
    http, _ = rig
    response = http.get(
        f"{BASE}/conversations",
        headers={"authorization": "Bearer good-token"},
        params={"limit": 500},
    )
    assert response.status_code == 422


def test_detail_masks_for_a_viewer(rig: tuple[TestClient, FakeLogs]) -> None:
    http, logs = rig
    response = http.get(
        f"{BASE}/conversations/{CONVERSATION_ID}", headers={"authorization": "Bearer good-token"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["messages"][0]["role"] == "assistant"
    assert body["summary"]["version"] == 2
    assert logs.calls[0] == ("detail", UUID(str(APP_ID)), UUID(str(CONVERSATION_ID)), True)


def test_detail_is_unmasked_for_an_app_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    logs = FakeLogs()
    app = create_app()
    app.state.chat_runtime = FakeRuntime()
    app.state.authorizer = FakeAuthorizer()
    monkeypatch.setattr(admin_router, "ConversationLogs", lambda *a, **k: logs)
    http = TestClient(app)
    http.get(
        f"{BASE}/conversations/{CONVERSATION_ID}", headers={"authorization": "Bearer good-token"}
    )
    assert logs.calls[0][3] is False


def test_detail_404_is_mapped(rig: tuple[TestClient, FakeLogs]) -> None:
    http, logs = rig
    logs.error = NotFoundError(
        message="会话不存在", code="CONVERSATION_NOT_FOUND", details={"id": str(CONVERSATION_ID)}
    )
    response = http.get(
        f"{BASE}/conversations/{CONVERSATION_ID}", headers={"authorization": "Bearer good-token"}
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "CONVERSATION_NOT_FOUND"


def test_release_passes_the_actor_and_note(rig: tuple[TestClient, FakeLogs]) -> None:
    http, logs = rig
    response = http.post(
        f"{BASE}/conversations/{CONVERSATION_ID}/release",
        headers={"authorization": "Bearer good-token"},
        json={"note": "坐席下班"},
    )
    assert response.status_code == 200
    assert response.json()["handoff_end_reason"] == "admin"
    call = logs.calls[0]
    assert call[0] == "force_release"
    assert call[3] == USER_ID
    assert call[4] == "坐席下班"


def test_release_conflict_is_409(rig: tuple[TestClient, FakeLogs]) -> None:
    http, logs = rig
    logs.error = ConflictError(
        message="会话当前不在转人工中",
        code="HANDOFF_STATE_CONFLICT",
        details={"handoff_status": "released"},
    )
    response = http.post(
        f"{BASE}/conversations/{CONVERSATION_ID}/release",
        headers={"authorization": "Bearer good-token"},
        json={},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "HANDOFF_STATE_CONFLICT"


def test_release_rejects_unknown_fields(rig: tuple[TestClient, FakeLogs]) -> None:
    http, _ = rig
    response = http.post(
        f"{BASE}/conversations/{CONVERSATION_ID}/release",
        headers={"authorization": "Bearer good-token"},
        json={"notes": "typo"},
    )
    assert response.status_code == 422


def test_callbacks_pass_include_cleared(rig: tuple[TestClient, FakeLogs]) -> None:
    http, logs = rig
    response = http.get(
        f"{BASE}/callbacks",
        headers={"authorization": "Bearer good-token"},
        params={"include_cleared": "true", "limit": 10},
    )
    assert response.status_code == 200
    assert logs.calls[0] == ("callbacks", UUID(str(APP_ID)), None, 10, True)


def test_clear_callback_passes_note_and_actor(rig: tuple[TestClient, FakeLogs]) -> None:
    http, logs = rig
    response = http.post(
        f"{BASE}/callbacks/{CONVERSATION_ID}",
        headers={"authorization": "Bearer good-token"},
        json={"note": "已电话回复"},
    )
    assert response.status_code == 200
    assert response.json()["needs_callback"] is False
    assert logs.calls[0] == (
        "clear_callback",
        UUID(str(APP_ID)),
        UUID(str(CONVERSATION_ID)),
        "已电话回复",
        USER_ID,
    )


def test_missing_token_is_401(rig: tuple[TestClient, FakeLogs]) -> None:
    http, _ = rig
    assert http.get(f"{BASE}/conversations").status_code == 401


def test_log_read_permission_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app()
    app.state.chat_runtime = FakeRuntime()
    app.state.authorizer = FakeAuthorizer(deny="log.read")
    monkeypatch.setattr(admin_router, "ConversationLogs", lambda *a, **k: FakeLogs())
    response = TestClient(app).get(
        f"{BASE}/conversations", headers={"authorization": "Bearer good-token"}
    )
    assert response.status_code == 403


def test_release_requires_its_own_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app()
    app.state.chat_runtime = FakeRuntime()
    app.state.authorizer = FakeAuthorizer(deny="handoff.release")
    monkeypatch.setattr(admin_router, "ConversationLogs", lambda *a, **k: FakeLogs())
    response = TestClient(app).post(
        f"{BASE}/conversations/{CONVERSATION_ID}/release",
        headers={"authorization": "Bearer good-token"},
        json={},
    )
    assert response.status_code == 403
