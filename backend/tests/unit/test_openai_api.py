"""M3.3 ③ OpenAI 兼容接口：纯函数（解析 / 应答 / 错误体 / 流式）与 `/v1/*` 路由。

路由层的假实现与 `test_knowledge_api.py` 同款：`create_app()` + 手动装 `app.state.authenticator`，
再用 `monkeypatch` 把 `openai_router._service` 换成假服务，因此不需要数据库与模型。
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from aics.core.errors import ForbiddenError, UnauthenticatedError, ValidationAppError
from aics.main import create_app
from aics.modules.chat import openai, openai_router
from aics.modules.chat.service import ErrorEvent, StartEvent, TurnResult, clamp_overrides
from aics.rag.ports import ChatTurn
from aics.rag.schemas import AppRagConfig, DeltaEvent, TraceEvent

APP = UUID("01920000-0000-7000-8000-0000000000aa")
OTHER = UUID("01920000-0000-7000-8000-0000000000bb")


def body(**kw: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "model": str(APP),
        "messages": [{"role": "user", "content": "退货怎么退？"}],
    }
    out.update(kw)
    return out


def request_of(**kw: Any) -> openai.ChatCompletionRequest:
    return openai.ChatCompletionRequest.model_validate(body(**kw))


def make_result(**kw: Any) -> TurnResult:
    base: dict[str, Any] = {
        "conversation_id": uuid4(),
        "previous_conversation_id": None,
        "message_id": uuid4(),
        "user_message_id": uuid4(),
        "reply_by": "ai",
        "handoff_status": "none",
        "answer": "7 天内可以退回。",
        "answer_type": "answer",
        "intent": {"name": "kb_qa", "confidence": 0.9, "source": "llm"},
        "citations": [{"n": 1, "document_id": str(uuid4()), "document_title": "退货政策"}],
        "prompt_tokens": 120,
        "completion_tokens": 30,
    }
    base.update(kw)
    return TurnResult(**base)


class FakeHandle:
    """假 TurnHandle：`result()` 给阻塞应答，`next_event()` 按脚本喂事件。"""

    def __init__(self, result: TurnResult, events: list[Any] | None = None, pings: int = 0):
        self._result = result
        self._events = list(events or [])
        self._pings = pings
        self.cancelled = False

    async def result(self) -> TurnResult:
        return self._result

    async def next_event(self, timeout_s: float | None = None) -> Any:
        if self._pings:
            self._pings -= 1
            raise TimeoutError
        return self._events.pop(0) if self._events else None

    def cancel(self) -> None:
        self.cancelled = True


class FakeService:
    def __init__(self, handle: FakeHandle):
        self.handle = handle
        self.calls: list[Any] = []

    async def start_turn(self, req: Any, *, t0: float | None = None) -> FakeHandle:
        self.calls.append(req)
        return self.handle


class FakeAuth:
    """`/v1/*` 走 `authenticate_key`（没有路径里的 app_id）。"""

    def __init__(self, *, app_id: UUID = APP, scopes: tuple[str, ...] = ("chat",), fail: str = ""):
        self.app_id = app_id
        self.key_id = uuid4()
        self.key_prefix = "aics_deadbeef"
        self.scopes = frozenset(scopes)
        self.fail = fail

    def _principal(self) -> Any:
        from aics.core.security import Principal

        return Principal(
            app_id=self.app_id,
            key_id=self.key_id,
            key_prefix=self.key_prefix,
            scopes=self.scopes,
            rate_headers={"X-RateLimit-Limit": "60", "X-RateLimit-Remaining": "59"},
        )

    async def authenticate_key(self, authorization: str | None, **kw: Any) -> Any:
        if not authorization:
            raise UnauthenticatedError()
        wanted = set(kw.get("any_scope") or ())
        if wanted and not (self.scopes & wanted):
            raise ForbiddenError(details={"required_scope": sorted(wanted)})
        return self._principal()

    async def authenticate(self, authorization: str | None, **kw: Any) -> Any:
        return await self.authenticate_key(authorization, **kw)


def client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: TurnResult | None = None,
    events: list[Any] | None = None,
    pings: int = 0,
    auth: FakeAuth | None = None,
) -> tuple[TestClient, FakeService, FakeAuth]:
    service = FakeService(FakeHandle(result or make_result(), events, pings))
    fake = auth or FakeAuth()
    app = create_app()
    app.state.authenticator = fake
    monkeypatch.setattr(openai_router, "_service", lambda request: service)
    return TestClient(app, raise_server_exceptions=False), service, fake


def auth_header() -> dict[str, str]:
    return {"Authorization": "Bearer aics_test_key_value"}


# ---------------------------------------------------------------- 纯函数：解析
@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("  退货  ", "退货"),
        ([{"type": "text", "text": "退货"}], "退货"),
        ([{"type": "text", "text": "退"}, {"type": "text", "text": "货"}], "退\n货"),
        ([{"type": "image_url", "text": "x"}], ""),
        ([], ""),
        (None, ""),
    ],
)
def test_content_text(content: Any, expected: str) -> None:
    assert openai.content_text(openai.ChatMessage(role="user", content=content)) == expected


def test_parse_requires_a_user_last_message() -> None:
    with pytest.raises(ValidationAppError) as e:
        openai.parse(
            request_of(
                messages=[{"role": "user", "content": "a"}, {"role": "system", "content": "b"}]
            )
        )
    assert e.value.status == 422 and e.value.details["errors"][0]["loc"] == ["body", "messages"]


def test_parse_splits_history_and_ignores_system_and_empty_messages() -> None:
    parsed = openai.parse(
        request_of(
            messages=[
                {"role": "system", "content": "忽略我"},
                {"role": "user", "content": "第一问"},
                {"role": "assistant", "content": "第一答"},
                {"role": "assistant", "content": "   "},
                {"role": "user", "content": "第二问"},
                {"role": "user", "content": "现在的问题"},
            ],
            user="  u-42  ",
        )
    )
    assert parsed.question == "现在的问题"
    assert parsed.history == [
        ChatTurn(role="user", content="第一问"),
        ChatTurn(role="assistant", content="第一答"),
        ChatTurn(role="user", content="第二问"),
    ]
    assert parsed.user == "u-42"


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [{"role": "user", "content": "   "}],
        [{"role": "user", "content": "x" * 2001}],
        [{"role": "user", "content": str(i)} for i in range(201)],
    ],
)
def test_parse_rejections(messages: list[dict[str, Any]]) -> None:
    with pytest.raises(ValidationAppError):
        openai.parse(request_of(messages=messages))


def test_parse_ignores_tools_and_functions() -> None:
    parsed = openai.parse(request_of(tools=[{"type": "function"}], functions=[], n=3))
    assert parsed.question == "退货怎么退？"


def test_overrides_only_keeps_the_two_supported_keys() -> None:
    assert openai.overrides(request_of()) == {}
    assert openai.overrides(request_of(temperature=0.1, max_tokens=50, n=2)) == {
        "temperature": 0.1,
        "max_tokens": 50,
    }


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_parse_conversation_id_ignores_blank(raw: str | None) -> None:
    assert openai.parse_conversation_id(raw) is None


@pytest.mark.parametrize("raw", ["not-a-uuid", "0192-0000"])
def test_parse_conversation_id_rejects_garbage(raw: str) -> None:
    with pytest.raises(ValidationAppError) as e:
        openai.parse_conversation_id(raw)
    assert e.value.details["errors"][0]["loc"] == ["body", "X-AICS-Conversation-Id"]


def test_parse_conversation_id_accepts_a_uuid() -> None:
    cid = uuid4()
    assert openai.parse_conversation_id(str(cid)) == cid


# ---------------------------------------------------------------- 纯函数：应答与错误体
def test_completion_body_shape() -> None:
    r = make_result()
    out = openai.completion_body(r, model=str(APP))
    assert out["id"].startswith("chatcmpl-")
    assert out["object"] == "chat.completion" and out["model"] == str(APP)
    assert isinstance(out["created"], int)
    choice = out["choices"][0]
    assert choice["message"] == {"role": "assistant", "content": "7 天内可以退回。"}
    assert choice["finish_reason"] == "stop" and choice["index"] == 0
    assert out["usage"] == {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}
    assert out["aics"]["conversation_id"] == r.conversation_id
    assert out["aics"]["reply_by"] == "ai" and out["aics"]["handoff"] is False
    assert out["aics"]["citations"] == r.citations


def test_completion_body_keeps_an_empty_answer_for_a_human_agent() -> None:
    out = openai.completion_body(make_result(answer=None, reply_by="none"), model=str(APP))
    assert out["choices"][0]["message"]["content"] == ""
    assert out["choices"][0]["finish_reason"] == "stop"
    assert out["aics"]["reply_by"] == "none"


def test_chunk_carries_delta_and_optional_aics() -> None:
    plain = openai.chunk(cid="chatcmpl-1", model=str(APP), created=1, delta={"content": "你"})
    assert plain["object"] == "chat.completion.chunk" and plain["choices"][0]["delta"] == {
        "content": "你"
    }
    assert plain["choices"][0]["finish_reason"] is None and "aics" not in plain
    last = openai.chunk(
        cid="chatcmpl-1", model=str(APP), created=1, delta={}, finish_reason="stop", aics={"x": 1}
    )
    assert last["choices"][0]["finish_reason"] == "stop" and last["aics"] == {"x": 1}


@pytest.mark.parametrize(
    ("code", "status", "expected_code", "expected_type"),
    [
        ("APP_NOT_FOUND", 404, "model_not_found", "invalid_request_error"),
        ("CONVERSATION_NOT_FOUND", 404, "conversation_not_found", "invalid_request_error"),
        ("UNAUTHENTICATED", 401, "unauthenticated", "authentication_error"),
        ("FORBIDDEN", 403, "forbidden", "permission_error"),
        ("RATE_LIMITED", 429, "rate_limited", "rate_limit_error"),
        ("UPSTREAM_UNAVAILABLE", 503, "upstream_unavailable", "api_error"),
        ("INTERNAL_ERROR", 500, "internal_error", "api_error"),
        ("WEIRD", 418, "weird", "invalid_request_error"),
    ],
)
def test_error_body_mapping(code: str, status: int, expected_code: str, expected_type: str) -> None:
    assert openai.error_body(code, "msg", status) == {
        "error": {"message": "msg", "type": expected_type, "code": expected_code}
    }


# ---------------------------------------------------------------- 纯函数：流式编码
async def collect(handle: FakeHandle, **kw: Any) -> list[str]:
    return [part async for part in openai.stream(handle, model=str(APP), **kw)]


async def test_stream_emits_role_deltas_final_chunk_and_done() -> None:
    r = make_result()
    handle = FakeHandle(
        r,
        [
            StartEvent(r.conversation_id, r.message_id, None),
            DeltaEvent(text="7 天"),
            DeltaEvent(text="内"),
            r,
        ],
    )
    parts = await collect(handle)
    assert parts[0].startswith("data: {") and '"role":"assistant"' in parts[0]
    assert '"content":"7 天"' in parts[1] and '"content":"内"' in parts[2]
    final = json.loads(parts[3].removeprefix("data: ").strip())
    assert final["choices"][0]["finish_reason"] == "stop"
    assert final["aics"]["message_id"] == str(r.message_id)
    assert final["aics"]["usage"]["total_tokens"] == 150
    assert parts[-1] == "data: [DONE]\n\n"
    assert handle.cancelled is True


async def test_stream_emits_an_error_chunk_then_done() -> None:
    handle = FakeHandle(
        make_result(), [DeltaEvent(text="部分"), ErrorEvent("UPSTREAM_TIMEOUT", "超时", 504)]
    )
    parts = await collect(handle)
    err = json.loads(parts[1].removeprefix("data: ").strip())
    assert err["error"]["code"] == "upstream_timeout" and err["error"]["type"] == "api_error"
    assert parts[-1] == "data: [DONE]\n\n"


async def test_stream_sends_a_heartbeat_comment_while_idle() -> None:
    handle = FakeHandle(make_result(), [DeltaEvent(text="慢")], pings=1)
    parts = await collect(handle, heartbeat_s=0.01)
    assert parts[0] == ": ping\n\n" and "慢" in parts[1]


async def test_stream_drops_trace_events() -> None:
    r = make_result()
    handle = FakeHandle(r, [TraceEvent(trace={"intent": "kb_qa"}), DeltaEvent(text="答"), r])
    parts = await collect(handle)
    assert len(parts) == 3  # delta + 末 chunk + [DONE]
    assert "trace" not in parts[0]


async def test_stream_without_events_ends_silently() -> None:
    """队列直接关闭（没有终结事件）时不补 `[DONE]`：真实链路里终结事件一定先到。"""
    assert await collect(FakeHandle(make_result())) == []


# ---------------------------------------------------------------- 路由
def test_models_lists_the_key_app(monkeypatch: pytest.MonkeyPatch) -> None:
    c, _, _ = client(monkeypatch)
    res = c.get("/v1/models", headers=auth_header())
    assert res.status_code == 200
    assert res.json() == {
        "object": "list",
        "data": [{"id": str(APP), "object": "model", "owned_by": "aics"}],
    }
    assert res.headers["x-ratelimit-limit"] == "60"


def test_models_without_a_key_uses_the_openai_error_body(monkeypatch: pytest.MonkeyPatch) -> None:
    c, _, _ = client(monkeypatch)
    res = c.get("/v1/models")
    assert res.status_code == 401
    assert res.json()["error"]["type"] == "authentication_error"
    assert res.json()["error"]["code"] == "unauthenticated"


def test_chat_completions_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    c, service, fake = client(monkeypatch)
    res = c.post(
        "/v1/chat/completions",
        json=body(
            messages=[
                {"role": "user", "content": "第一问"},
                {"role": "assistant", "content": "第一答"},
                {"role": "user", "content": "现在的问题"},
            ],
            user="u-1",
            temperature=0.1,
            max_tokens=100,
        ),
        headers=auth_header(),
    )
    assert res.status_code == 200
    payload = res.json()
    assert payload["object"] == "chat.completion"
    assert payload["choices"][0]["message"]["content"] == "7 天内可以退回。"
    assert UUID(payload["aics"]["conversation_id"])
    sent = service.calls[0]
    assert sent.app_id == APP and sent.question == "现在的问题" and sent.channel == "openai"
    assert sent.api_key_id == fake.key_id and sent.external_user_id == "u-1"
    assert sent.conversation_id is None
    assert sent.inline_history == [
        ChatTurn(role="user", content="第一问"),
        ChatTurn(role="assistant", content="第一答"),
    ]
    assert sent.rag_overrides == {"temperature": 0.1, "max_tokens": 100}


def test_chat_completions_rejects_a_foreign_model(monkeypatch: pytest.MonkeyPatch) -> None:
    c, service, _ = client(monkeypatch)
    res = c.post("/v1/chat/completions", json=body(model=str(OTHER)), headers=auth_header())
    assert res.status_code == 404
    assert res.json() == {
        "error": {
            "message": openai_router.MODEL_MISMATCH,
            "type": "invalid_request_error",
            "code": "model_not_found",
        }
    }
    assert service.calls == []


def test_chat_completions_requires_the_chat_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    c, _, _ = client(monkeypatch, auth=FakeAuth(scopes=("kb:read",)))
    res = c.post("/v1/chat/completions", json=body(), headers=auth_header())
    assert res.status_code == 403
    assert res.json()["error"]["type"] == "permission_error"


def test_chat_completions_validates_the_body(monkeypatch: pytest.MonkeyPatch) -> None:
    c, _, _ = client(monkeypatch)
    res = c.post("/v1/chat/completions", json=body(messages=[]), headers=auth_header())
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "invalid_request"
    assert res.json()["error"]["type"] == "invalid_request_error"


def test_chat_completions_passes_the_conversation_header(monkeypatch: pytest.MonkeyPatch) -> None:
    c, service, _ = client(monkeypatch)
    cid = uuid4()
    headers = {**auth_header(), "X-AICS-Conversation-Id": str(cid)}
    assert c.post("/v1/chat/completions", json=body(), headers=headers).status_code == 200
    assert service.calls[0].conversation_id == cid


def test_chat_completions_rejects_a_bad_conversation_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c, service, _ = client(monkeypatch)
    headers = {**auth_header(), "X-AICS-Conversation-Id": "nope"}
    res = c.post("/v1/chat/completions", json=body(), headers=headers)
    assert res.status_code == 422 and res.json()["error"]["code"] == "invalid_request"
    assert service.calls == []


def test_chat_completions_streams_sse(monkeypatch: pytest.MonkeyPatch) -> None:
    r = make_result()
    events = [StartEvent(r.conversation_id, r.message_id, None), DeltaEvent(text="7 天"), r]
    c, _, _ = client(monkeypatch, result=r, events=events)
    res = c.post("/v1/chat/completions", json=body(stream=True), headers=auth_header())
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    assert res.headers["cache-control"] == "no-cache"
    assert res.headers["x-accel-buffering"] == "no"
    lines = [ln for ln in res.text.split("\n\n") if ln]
    assert lines[-1] == "data: [DONE]"
    assert '"content":"7 天"' in lines[1]
    final = json.loads(lines[2].removeprefix("data: "))
    assert final["aics"]["conversation_id"] == str(r.conversation_id)


def test_native_routes_keep_the_plain_error_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """`/v1` 分流不能改动原生接口的错误体（回归）。"""

    class Failing(FakeAuth):
        async def authenticate(self, authorization: str | None, **kw: Any) -> Any:
            raise UnauthenticatedError()

    c, _, _ = client(monkeypatch, auth=Failing())
    res = c.post(f"/api/v1/apps/{APP}/chat", json={"question": "你好"}, headers=auth_header())
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "UNAUTHENTICATED"
    assert "request_id" in res.json()["error"] and "type" not in res.json()["error"]


# ---------------------------------------------------------------- 请求级参数只收窄（chat.service）
@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"temperature": 0.9, "max_tokens": 5000}, {"temperature": 0.2, "max_tokens": 800}),
        ({"temperature": 0.0, "max_tokens": 1}, {"temperature": 0.0, "max_tokens": 1}),
        ({"temperature": -1.0}, {}),
        ({"max_tokens": 0}, {}),
        ({"temperature": "hot", "max_tokens": True}, {}),
        ({"top_k": 5, "fusion": "rrf"}, {}),
        ({}, {}),
    ],
)
def test_clamp_overrides_only_narrows(overrides: dict[str, Any], expected: dict[str, Any]) -> None:
    cfg = AppRagConfig()  # temperature=0.2, max_tokens=800
    assert clamp_overrides(cfg, overrides) == expected
