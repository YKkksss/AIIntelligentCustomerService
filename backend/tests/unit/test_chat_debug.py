"""M4.4 ②：调试对话接口（`/api/v1/admin/apps/{app_id}/debug/*`）。

业务链路（真 SSE、真 trace、`jobs(type=compact)`）由集成用例覆盖；这里证明三件事：
临时 `debug_config` 的合并与字段路径校验、viewer 的脱敏开关、router 把参数与用户交给
service / DebugChat，并按约定序列化。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from aics.core.errors import (
    ConversationNotFoundError,
    ForbiddenError,
    UnauthenticatedError,
    ValidationAppError,
)
from aics.core.security import AuthedUser
from aics.db.models.job import Job
from aics.main import create_app
from aics.modules.chat import admin_router
from aics.modules.chat.admin_router import DebugChat
from aics.modules.chat.debug import (
    DebugChat as DebugChatImpl,
)
from aics.modules.chat.debug import (
    masked_trace,
    merge_debug_config,
    message_dict,
)
from aics.modules.chat.service import StartEvent, TurnRequest
from aics.rag.config import AppRagConfig
from aics.rag.schemas import RunMode

APP_ID = uuid4()
CONVERSATION_ID = uuid4()


# ------------------------------------------------------------------ helpers
def test_merge_debug_config_deep_merges_and_keeps_defaults() -> None:
    base = AppRagConfig()
    merged = merge_debug_config(base, {"vector_top_k": 3, "temperature": 0.9})

    assert merged.vector_top_k == 3
    assert merged.temperature == 0.9
    # 未提交的字段保持应用配置
    assert merged.vector_weight == base.vector_weight
    assert merged.enable_rerank == base.enable_rerank


def test_merge_debug_config_reports_field_path() -> None:
    with pytest.raises(ValidationAppError) as exc:
        merge_debug_config(AppRagConfig(), {"vector_top_k": 0})

    errors = exc.value.details["errors"]
    assert errors[0]["loc"] == ["debug_config", "vector_top_k"]


def test_merge_debug_config_rejects_unknown_field() -> None:
    with pytest.raises(ValidationAppError) as exc:
        merge_debug_config(AppRagConfig(), {"nope": 1})

    assert exc.value.details["errors"][0]["loc"] == ["debug_config", "nope"]


def test_merge_debug_config_rejects_non_mapping() -> None:
    with pytest.raises(ValidationAppError) as exc:
        merge_debug_config(AppRagConfig(), ["top_k"])  # type: ignore[arg-type]

    assert exc.value.details["errors"][0]["loc"] == ["debug_config"]


def test_masked_trace_masks_phones_recursively() -> None:
    trace = {
        "retrieval": {"query": "帮我查 13812345678 的订单", "hits": [{"text": "13000001111"}]},
        "timings": {"total_ms": 12},
        "degraded": ["vector"],
    }

    masked = masked_trace(trace)

    assert masked is not None
    assert masked["retrieval"]["query"] == "帮我查 138****5678 的订单"
    assert masked["retrieval"]["hits"][0]["text"] == "130****1111"
    assert masked["timings"] == {"total_ms": 12}
    assert masked_trace(None) is None


@dataclass
class _Msg:
    id: UUID
    role: str = "assistant"
    kind: str = "chat"
    content: str = "客服 13812345678 为您服务"
    answer_type: str | None = "kb_qa"
    intent: str | None = "faq"
    intent_confidence: float | None = 0.8
    intent_source: str | None = "rules"
    citations: list[dict[str, Any]] | None = None
    handoff: bool = False
    interrupted: bool = False
    retrieval_trace: dict[str, Any] | None = None
    created_at: datetime = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


def test_message_dict_masks_only_for_read_only_members() -> None:
    msg = _Msg(
        id=uuid4(),
        citations=[{"n": 1, "text": "拨打 13900002222"}],
        retrieval_trace={"query": "13900002222"},
    )

    masked = message_dict(msg, mask=True)
    raw = message_dict(msg, mask=False)

    assert masked["content"] == "客服 138****5678 为您服务"
    assert masked["citations"][0]["text"] == "拨打 139****2222"
    assert masked["trace"]["query"] == "139****2222"
    assert raw["content"] == "客服 13812345678 为您服务"
    assert raw["trace"] == {"query": "13900002222"}


# ------------------------------------------------------------------ API
class FakeHandle:
    def __init__(self, events: list[Any]) -> None:
        self._events = list(events)
        self.cancelled = False

    async def next_event(self, timeout: float) -> Any:  # noqa: ASYNC109
        return self._events.pop(0) if self._events else None

    def cancel(self) -> None:
        self.cancelled = True


class FakeService:
    def __init__(self) -> None:
        self.requests: list[TurnRequest] = []
        self.handle = FakeHandle([StartEvent(CONVERSATION_ID, uuid4(), None)])

    async def start_turn(self, req: TurnRequest, *, t0: float | None = None) -> FakeHandle:
        self.requests.append(req)
        return self.handle


class FakeDebugChat:
    def __init__(self, runtime: Any = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.error: Exception | None = None

    async def history(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("history", kw))
        if self.error is not None:
            raise self.error
        return {
            "conversation": {
                "id": kw["conversation_id"],
                "channel": "debug",
                "status": "active",
                "handoff_status": "none",
                "close_reason": None,
                "closed_at": None,
                "created_at": datetime(2026, 10, 20, 12, 0, tzinfo=UTC),
                "last_activity_at": datetime(2026, 10, 20, 12, 0, tzinfo=UTC),
            },
            "items": [_Msg(id=uuid4()).__dict__ | {"trace": None}],
        }

    async def compact(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("compact", kw))
        if self.error is not None:
            raise self.error
        return {
            "job_id": uuid4(),
            "compacted": True,
            "version": 3,
            "batches": 1,
            "until_msg_id": uuid4(),
            "facts": 2,
            "usage": {"prompt": 10, "completion": 5},
        }


class FakeRuntime:
    def __init__(self) -> None:
        self.memory = object()


class FakeAuthorizer:
    def __init__(self, deny: str | None = None) -> None:
        self.deny = deny
        self.user_id = uuid4()

    async def authenticate(self, authorization: str | None) -> AuthedUser:
        if authorization != "Bearer good-token":
            raise UnauthenticatedError(details={"reason": "missing_token"})
        return AuthedUser(
            id=self.user_id, email="admin@example.com", display_name="管理员", is_super_admin=False
        )

    async def authorize(self, user: Any, *, app_id: Any, permission: str) -> None:
        if self.deny is not None and permission == self.deny:
            raise ForbiddenError(details={"reason": "missing_permission", "permission": permission})


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    service = FakeService()
    debug = FakeDebugChat()
    authorizer = FakeAuthorizer()
    monkeypatch.setattr(admin_router, "ChatService", lambda runtime: service)
    monkeypatch.setattr(admin_router, "DebugChat", lambda runtime: debug)
    app = create_app()
    app.state.chat_runtime = FakeRuntime()
    app.state.authorizer = authorizer
    return {
        "http": TestClient(app),
        "service": service,
        "debug": debug,
        "authorizer": authorizer,
        "headers": {"authorization": "Bearer good-token"},
    }


def test_debug_chat_pins_channel_and_mode(env: dict[str, Any]) -> None:
    resp = env["http"].post(
        f"/api/v1/admin/apps/{APP_ID}/debug/chat",
        json={
            "question": "  怎么退货  ",
            "conversation_id": str(CONVERSATION_ID),
            "debug_config": {"retrieval": {"top_k": 3}},
        },
        headers=env["headers"],
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert "event: start" in resp.text
    req = env["service"].requests[0]
    assert req.question == "怎么退货"
    assert req.channel == "debug"
    assert req.run_mode == RunMode.debug
    assert req.debug_config == {"retrieval": {"top_k": 3}}
    assert req.created_by == env["authorizer"].user_id
    assert env["service"].handle.cancelled is True


def test_debug_chat_maps_validation_error(env: dict[str, Any]) -> None:
    async def boom(req: TurnRequest, *, t0: float | None = None) -> FakeHandle:
        raise ValidationAppError(
            message="调试参数校验失败",
            details={"errors": [{"loc": ["debug_config", "retrieval", "top_k"], "msg": "太小"}]},
        )

    env["service"].start_turn = boom  # type: ignore[method-assign]

    resp = env["http"].post(
        f"/api/v1/admin/apps/{APP_ID}/debug/chat",
        json={"question": "怎么退货", "debug_config": {"retrieval": {"top_k": 0}}},
        headers=env["headers"],
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["errors"][0]["loc"][0] == "debug_config"


def test_history_masked_for_read_only_member(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    env["authorizer"].deny = "app.write"

    resp = env["http"].get(
        f"/api/v1/admin/apps/{APP_ID}/debug/conversations/{CONVERSATION_ID}",
        headers=env["headers"],
    )

    assert resp.status_code == 200
    _, kw = env["debug"].calls[0]
    # 脱敏本身由 message_dict / masked_trace 的用例覆盖，这里只证明开关被打开
    assert kw["mask"] is True


def test_history_raw_for_app_member(env: dict[str, Any]) -> None:
    resp = env["http"].get(
        f"/api/v1/admin/apps/{APP_ID}/debug/conversations/{CONVERSATION_ID}",
        headers=env["headers"],
    )

    assert resp.status_code == 200
    assert env["debug"].calls[0][1]["mask"] is False


def test_history_maps_not_found(env: dict[str, Any]) -> None:
    env["debug"].error = ConversationNotFoundError(details={"id": str(CONVERSATION_ID)})

    resp = env["http"].get(
        f"/api/v1/admin/apps/{APP_ID}/debug/conversations/{CONVERSATION_ID}",
        headers=env["headers"],
    )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "CONVERSATION_NOT_FOUND"


def test_compact_passes_actor_and_serialises(env: dict[str, Any]) -> None:
    resp = env["http"].post(
        f"/api/v1/admin/apps/{APP_ID}/debug/compact",
        json={"conversation_id": str(CONVERSATION_ID)},
        headers=env["headers"],
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["batches"] == 1 and body["facts"] == 2
    _, kw = env["debug"].calls[0]
    assert kw["created_by"] == env["authorizer"].user_id
    assert kw["conversation_id"] == CONVERSATION_ID


def test_compact_rejects_unknown_field(env: dict[str, Any]) -> None:
    resp = env["http"].post(
        f"/api/v1/admin/apps/{APP_ID}/debug/compact",
        json={"conversation_id": str(CONVERSATION_ID), "oops": 1},
        headers=env["headers"],
    )

    assert resp.status_code == 422


def test_requires_debug_permission(env: dict[str, Any]) -> None:
    env["authorizer"].deny = "chat.debug"

    resp = env["http"].get(
        f"/api/v1/admin/apps/{APP_ID}/debug/conversations/{CONVERSATION_ID}",
        headers=env["headers"],
    )

    assert resp.status_code == 403
    assert resp.json()["error"]["details"]["permission"] == "chat.debug"


def test_missing_token_is_401(env: dict[str, Any]) -> None:
    resp = env["http"].post(f"/api/v1/admin/apps/{APP_ID}/debug/chat", json={"question": "hi"})

    assert resp.status_code == 401


__all__ = ["DebugChat", "DebugChatImpl", "Job", "ValidationError"]
