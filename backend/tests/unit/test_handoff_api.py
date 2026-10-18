"""M3.4 ②：坐席接口路由（开放接口参考 §5）—— 参数、序列化、scope 与错误码。

fake 认证器 + fake 运行时跑真路由（`create_app()` 挂载）：状态机分支由
`tests/unit/test_handoff_service.py` 覆盖，SQL 与并发由集成用例覆盖。
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
    ConversationNotFoundError,
    ForbiddenError,
    HandoffStateConflictError,
)
from aics.core.security import Principal
from aics.db.models.conversation import Message
from aics.main import create_app
from aics.modules.chat.repository import ConversationClosedError
from aics.modules.handoff.service import DEFAULT_PAGE_SIZE, HandoffItem, HandoffState

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
HOST = "/api/v1/apps/{app_id}"


class FakeAuth:
    """只校验 scope 是否被请求；真实校验由 `tests/unit/test_apikeys.py` 覆盖。"""

    def __init__(self, scopes: tuple[str, ...] = ("handoff:write",)) -> None:
        self.scopes = scopes
        self.key_id = uuid4()
        self.calls: list[tuple[UUID, tuple[str, ...]]] = []

    async def authenticate(
        self,
        authorization: str | None,
        *,
        app_id: UUID,
        any_scope: tuple[str, ...],
        client_ip: str | None = None,
        forwarded_for: str | None = None,
    ) -> Principal:
        self.calls.append((app_id, any_scope))
        if not set(self.scopes) & set(any_scope):
            raise ForbiddenError(details={"required_scope": list(any_scope)})
        return Principal(
            app_id=app_id,
            key_id=self.key_id,
            key_prefix="aics_test",
            scopes=frozenset(self.scopes),
            rate_headers={"X-RateLimit-Limit": "60", "X-RateLimit-Remaining": "59"},
        )


def _state(**kw: Any) -> HandoffState:
    fields: dict[str, Any] = {
        "handoff_status": "pending",
        "handoff_reason": "keyword",
        "requested_at": NOW,
        "accepted_at": None,
        "released_at": None,
        "handoff_end_reason": None,
        "needs_callback": False,
        "agent_id": None,
        "agent_name": None,
        "handoff_ai_mode": "continue",
        "in_service_hours": True,
    }
    fields.update(kw)
    return HandoffState(**fields)


def _item(**kw: Any) -> HandoffItem:
    fields: dict[str, Any] = {
        "conversation_id": uuid4(),
        "handoff_status": "pending",
        "handoff_reason": "keyword",
        "requested_at": NOW,
        "accepted_at": None,
        "agent_id": None,
        "agent_name": None,
        "needs_callback": False,
        "message_count": 4,
        "last_activity_at": NOW,
        "external_user_id": "u_1",
    }
    fields.update(kw)
    return HandoffItem(**fields)


def _message(app_id: UUID, **kw: Any) -> Message:
    fields: dict[str, Any] = {
        "id": uuid4(),
        "app_id": app_id,
        "conversation_id": uuid4(),
        "role": "human_agent",
        "kind": "chat",
        "content": "我帮您查一下",
        "agent_id": "a_001",
        "created_at": NOW,
    }
    fields.update(kw)
    return Message(**fields)


class FakeService:
    def __init__(
        self,
        *,
        state: HandoffState | None = None,
        items: list[HandoffItem] | None = None,
        more: bool = False,
        message: Message | None = None,
        error: Exception | None = None,
    ) -> None:
        self.state_ = state or _state()
        self.items = list(items or [])
        self.more = more
        self.message = message
        self.error = error
        self.calls: list[tuple[Any, ...]] = []

    def _fail(self) -> None:
        if self.error is not None:
            raise self.error

    async def state(self, app_id: UUID, conversation_id: UUID) -> HandoffState:
        self.calls.append(("state", app_id, conversation_id))
        self._fail()
        return self.state_

    async def list_open(
        self,
        app_id: UUID,
        *,
        status: str,
        limit: int,
        after: UUID | None = None,
    ) -> tuple[list[HandoffItem], bool]:
        self.calls.append(("list_open", app_id, status, limit, after))
        self._fail()
        return self.items, self.more

    async def accept(
        self,
        app_id: UUID,
        conversation_id: UUID,
        *,
        agent_id: str,
        agent_name: str | None = None,
    ) -> HandoffState:
        self.calls.append(("accept", app_id, conversation_id, agent_id, agent_name))
        self._fail()
        return self.state_

    async def post_message(
        self, app_id: UUID, conversation_id: UUID, *, agent_id: str, content: str
    ) -> Message:
        self.calls.append(("post_message", app_id, conversation_id, agent_id, content))
        self._fail()
        return self.message or _message(app_id, conversation_id=conversation_id)

    async def release(
        self,
        app_id: UUID,
        conversation_id: UUID,
        *,
        agent_id: str,
        note: str | None = None,
    ) -> HandoffState:
        self.calls.append(("release", app_id, conversation_id, agent_id, note))
        self._fail()
        return self.state_


class FakeRuntime:
    def __init__(self, service: FakeService) -> None:
        self.service_ = service

    @asynccontextmanager
    async def session(self) -> AsyncIterator[None]:
        yield None

    def service(self, session: Any) -> FakeService:  # noqa: ARG002 - 与 HandoffRuntime 同形
        return self.service_


def _client(
    service: FakeService, *, scopes: tuple[str, ...] = ("handoff:write",)
) -> tuple[TestClient, FakeAuth]:
    app = create_app()
    auth = FakeAuth(scopes)
    app.state.authenticator = auth
    app.state.handoff_runtime = FakeRuntime(service)
    return TestClient(app, raise_server_exceptions=False), auth


def test_get_state_returns_the_contract_fields() -> None:
    app_id, conversation_id = uuid4(), uuid4()
    service = FakeService(state=_state(handoff_status="accepted", agent_id="a_001"))
    client, auth = _client(service)

    res = client.get(
        f"/api/v1/apps/{app_id}/conversations/{conversation_id}/handoff",
        headers={"Authorization": "Bearer aics_x"},
    )

    assert res.status_code == 200
    assert res.headers["X-RateLimit-Remaining"] == "59"
    assert res.json() == {
        "handoff_status": "accepted",
        "handoff_reason": "keyword",
        "requested_at": "2026-10-17T12:00:00+00:00",
        "accepted_at": None,
        "released_at": None,
        "handoff_end_reason": None,
        "needs_callback": False,
        "agent_id": "a_001",
        "agent_name": None,
        "handoff_ai_mode": "continue",
        "in_service_hours": True,
    }
    assert service.calls == [("state", app_id, conversation_id)]
    # 状态查询同时接受 chat 与 handoff:write
    assert auth.calls[0][1] == ("chat", "handoff:write")


def test_read_accepts_a_chat_only_key() -> None:
    service = FakeService()
    client, _ = _client(service, scopes=("chat",))

    res = client.get(
        f"/api/v1/apps/{uuid4()}/conversations/{uuid4()}/handoff",
        headers={"Authorization": "Bearer aics_x"},
    )

    assert res.status_code == 200


@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("get", "/handoffs"),
        ("post", "/conversations/{cid}/handoff/accept"),
        ("post", "/conversations/{cid}/handoff/messages"),
        ("post", "/conversations/{cid}/handoff/release"),
    ],
)
def test_write_routes_reject_a_chat_only_key(method: str, suffix: str) -> None:
    """坐席接口必须用 handoff:write 的 Key；403 由鉴权层给出。"""
    service = FakeService()
    client, _ = _client(service, scopes=("chat",))
    url = f"/api/v1/apps/{uuid4()}{suffix.format(cid=uuid4())}"
    kwargs: dict[str, Any] = {"headers": {"Authorization": "Bearer aics_x"}}
    if method == "post":
        kwargs["json"] = {"agent_id": "a_001", "content": "您好"}

    res = getattr(client, method)(url, **kwargs)

    assert res.status_code == 403
    assert res.json()["error"]["code"] == "FORBIDDEN"
    assert service.calls == []


def test_accept_passes_the_agent_identity() -> None:
    app_id, conversation_id = uuid4(), uuid4()
    service = FakeService(state=_state(handoff_status="accepted"))
    client, _ = _client(service)

    res = client.post(
        f"/api/v1/apps/{app_id}/conversations/{conversation_id}/handoff/accept",
        json={"agent_id": "a_001", "agent_name": "小王"},
        headers={"Authorization": "Bearer aics_x"},
    )

    assert res.status_code == 200
    assert res.json()["handoff_status"] == "accepted"
    assert service.calls == [("accept", app_id, conversation_id, "a_001", "小王")]


def test_accept_defaults_the_agent_name() -> None:
    service = FakeService()
    client, _ = _client(service)

    res = client.post(
        f"/api/v1/apps/{uuid4()}/conversations/{uuid4()}/handoff/accept",
        json={"agent_id": "a_001"},
        headers={"Authorization": "Bearer aics_x"},
    )

    assert res.status_code == 200
    assert service.calls[0][4] is None


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"agent_id": ""},
        {"agent_id": "   "},
        {"agent_id": "a" * 129},
        {"agent_id": "a_001", "agent_name": "x" * 65},
        {"agent_id": "a_001", "extra": 1},
    ],
)
def test_accept_validates_the_body(body: dict[str, Any]) -> None:
    service = FakeService()
    client, _ = _client(service)

    res = client.post(
        f"/api/v1/apps/{uuid4()}/conversations/{uuid4()}/handoff/accept",
        json=body,
        headers={"Authorization": "Bearer aics_x"},
    )

    assert res.status_code == 422
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"
    assert service.calls == []


def test_accept_rejects_a_non_uuid_conversation_id() -> None:
    service = FakeService()
    client, _ = _client(service)

    res = client.post(
        f"/api/v1/apps/{uuid4()}/conversations/not-a-uuid/handoff/accept",
        json={"agent_id": "a_001"},
        headers={"Authorization": "Bearer aics_x"},
    )

    assert res.status_code == 422


def test_accept_conflict_maps_to_409() -> None:
    error = HandoffStateConflictError(details={"handoff_status": "accepted"})
    client, _ = _client(FakeService(error=error))

    res = client.post(
        f"/api/v1/apps/{uuid4()}/conversations/{uuid4()}/handoff/accept",
        json={"agent_id": "a_001"},
        headers={"Authorization": "Bearer aics_x"},
    )

    assert res.status_code == 409
    body = res.json()["error"]
    assert body["code"] == "HANDOFF_STATE_CONFLICT"
    assert body["details"] == {"handoff_status": "accepted"}
    assert body["request_id"]


def test_closed_conversation_maps_to_409() -> None:
    error = ConversationClosedError(details={"id": str(uuid4())})
    client, _ = _client(FakeService(error=error))

    res = client.post(
        f"/api/v1/apps/{uuid4()}/conversations/{uuid4()}/handoff/accept",
        json={"agent_id": "a_001"},
        headers={"Authorization": "Bearer aics_x"},
    )

    assert res.status_code == 409
    assert res.json()["error"]["code"] == "CONVERSATION_CLOSED"


def test_unknown_conversation_maps_to_404() -> None:
    client, _ = _client(FakeService(error=ConversationNotFoundError()))

    res = client.get(
        f"/api/v1/apps/{uuid4()}/conversations/{uuid4()}/handoff",
        headers={"Authorization": "Bearer aics_x"},
    )

    assert res.status_code == 404
    assert res.json()["error"]["code"] == "CONVERSATION_NOT_FOUND"


def test_post_message_returns_the_message_object() -> None:
    app_id, conversation_id = uuid4(), uuid4()
    service = FakeService(message=_message(app_id, conversation_id=conversation_id))
    client, _ = _client(service)

    res = client.post(
        f"/api/v1/apps/{app_id}/conversations/{conversation_id}/handoff/messages",
        json={"agent_id": "a_001", "content": "我帮您查一下"},
        headers={"Authorization": "Bearer aics_x"},
    )

    assert res.status_code == 201
    body = res.json()
    assert body["role"] == "human_agent"
    assert body["kind"] == "chat"
    assert body["content"] == "我帮您查一下"
    assert body["agent_id"] == "a_001"
    assert body["created_at"] == "2026-10-17T12:00:00+00:00"
    assert set(body) == {
        "id",
        "role",
        "kind",
        "content",
        "answer_type",
        "handoff",
        "intent",
        "citations",
        "agent_id",
        "interrupted",
        "created_at",
    }
    assert service.calls == [("post_message", app_id, conversation_id, "a_001", "我帮您查一下")]


@pytest.mark.parametrize(
    "body",
    [
        {"agent_id": "a_001", "content": ""},
        {"agent_id": "a_001", "content": "x" * 4001},
        {"agent_id": "a_001"},
        {"content": "您好"},
        {"agent_id": "a_001", "content": "您好", "extra": 1},
    ],
)
def test_post_message_validates_the_body(body: dict[str, Any]) -> None:
    service = FakeService()
    client, _ = _client(service)

    res = client.post(
        f"/api/v1/apps/{uuid4()}/conversations/{uuid4()}/handoff/messages",
        json=body,
        headers={"Authorization": "Bearer aics_x"},
    )

    assert res.status_code == 422
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"
    assert service.calls == []


def test_release_passes_the_note() -> None:
    app_id, conversation_id = uuid4(), uuid4()
    service = FakeService(state=_state(handoff_status="released", handoff_end_reason="agent"))
    client, _ = _client(service)

    res = client.post(
        f"/api/v1/apps/{app_id}/conversations/{conversation_id}/handoff/release",
        json={"agent_id": "a_001", "note": "客户要求转回机器人"},
        headers={"Authorization": "Bearer aics_x"},
    )

    assert res.status_code == 200
    assert res.json()["handoff_status"] == "released"
    assert res.json()["handoff_end_reason"] == "agent"
    assert service.calls == [("release", app_id, conversation_id, "a_001", "客户要求转回机器人")]


def test_release_rejects_a_too_long_note() -> None:
    service = FakeService()
    client, _ = _client(service)

    res = client.post(
        f"/api/v1/apps/{uuid4()}/conversations/{uuid4()}/handoff/release",
        json={"agent_id": "a_001", "note": "x" * 501},
        headers={"Authorization": "Bearer aics_x"},
    )

    assert res.status_code == 422
    assert service.calls == []


def test_list_handoffs_returns_items_and_a_cursor() -> None:
    app_id = uuid4()
    items = [_item(), _item(handoff_status="accepted", agent_id="a_001")]
    service = FakeService(items=items, more=True)
    client, _ = _client(service)

    res = client.get(
        f"/api/v1/apps/{app_id}/handoffs",
        params={"status": "accepted", "page_size": 2},
        headers={"Authorization": "Bearer aics_x"},
    )

    assert res.status_code == 200
    body = res.json()
    assert [item["id"] for item in body["items"]] == [str(i.conversation_id) for i in items]
    assert body["items"][0]["message_count"] == 4
    assert body["items"][0]["external_user_id"] == "u_1"
    assert body["next_cursor"]
    assert service.calls == [("list_open", app_id, "accepted", 2, None)]

    # 游标回传：服务层收到最后一条的 id
    res = client.get(
        f"/api/v1/apps/{app_id}/handoffs",
        params={"cursor": body["next_cursor"]},
        headers={"Authorization": "Bearer aics_x"},
    )
    assert res.status_code == 200
    assert service.calls[-1] == (
        "list_open",
        app_id,
        "pending",
        DEFAULT_PAGE_SIZE,
        items[-1].conversation_id,
    )


def test_list_handoffs_defaults_to_pending() -> None:
    service = FakeService(items=[_item()])
    client, _ = _client(service)

    res = client.get(f"/api/v1/apps/{uuid4()}/handoffs", headers={"Authorization": "Bearer aics_x"})

    assert res.status_code == 200
    assert res.json()["next_cursor"] is None  # more=False
    assert service.calls == [("list_open", service.calls[0][1], "pending", DEFAULT_PAGE_SIZE, None)]


@pytest.mark.parametrize(
    "params",
    [
        {"status": "none"},
        {"status": ""},
        {"page_size": 0},
        {"page_size": 101},
        {"cursor": "!!!"},
        {"cursor": "bm90LWEtY3Vyc29y"},  # "not-a-cursor"
    ],
)
def test_list_handoffs_validates_the_query(params: dict[str, Any]) -> None:
    service = FakeService()
    client, _ = _client(service)

    res = client.get(
        f"/api/v1/apps/{uuid4()}/handoffs",
        params=params,
        headers={"Authorization": "Bearer aics_x"},
    )

    assert res.status_code == 422
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"
    assert service.calls == []
