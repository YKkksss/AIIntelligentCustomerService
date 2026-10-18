"""M3.3 ④ 官方 openai SDK 端到端集成用例（真 PG + 假模型，不需要网络）。

用官方 SDK 而不是手写 HTTP，是为了验证对外承诺「客户端把 `base_url` 指到本服务就能用」：
请求解析、应答形态、流式分片、错误体，任何一处和 OpenAI 形态不一致这里都会失败。

用假知识库与假模型（`tests/chat_support.make_runtime`），所以不产生真实费用；需要真 PG 与
Redis，本机跑不了时按待复验清单在服务器上执行。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from openai import APIStatusError, AsyncOpenAI, AuthenticationError
from tests.chat_support import make_runtime

from aics.core.config import get_settings
from aics.main import create_app
from aics.modules.apikeys.service import ApiKeyAuthenticator, ApiKeyService

pytestmark = pytest.mark.integration

QUESTION = "退款多久到账？"
ANSWER = "退款 3–5 个工作日原路退回[1]。"
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_order",
            "description": "查询订单",
            "parameters": {"type": "object", "properties": {}},
        },
    }
]


async def new_key(pg: dict[str, Any], redis: Any, app_id: UUID) -> str:
    pepper = get_settings().api_key_pepper.get_secret_value()
    async with pg["sessions"]() as s:
        _, plain = await ApiKeyService(s, pepper=pepper, redis=redis).create(
            app_id, name="openai-sdk-it", scopes=["chat"], rpm_limit=1000
        )
        await s.commit()
    return plain


def build_app(pg: dict[str, Any], redis: Any) -> FastAPI:
    app = create_app()
    app.state.authenticator = ApiKeyAuthenticator(
        pg["sessions"],
        pepper=get_settings().api_key_pepper.get_secret_value(),
        redis=redis,
    )
    app.state.chat_runtime = make_runtime(pg["sessions"])
    return app


def extra(obj: Any, key: str) -> Any:
    """SDK 把未知字段放进 `model_extra`（`aics` 扩展就是这么回来的）。"""
    return (getattr(obj, "model_extra", None) or {}).get(key)


def error_code(exc: APIStatusError) -> str | None:
    """错误体是 `{"error": {..., "code": ...}}`：SDK 版本不同，可能已摊平也可能原样给。"""
    code = getattr(exc, "code", None)
    if code:
        return str(code)
    body = exc.body
    if isinstance(body, dict):
        inner = body.get("error")
        if isinstance(inner, dict):
            return inner.get("code")
        if "code" in body:
            return body["code"]
    return None


async def conversation_status(sdk: dict[str, Any], conversation_id: str) -> str | None:
    r = await sdk["http"].get(
        f"/api/v1/apps/{sdk['app_id']}/conversations/{conversation_id}/messages",
        headers={"Authorization": f"Bearer {sdk['key']}"},
    )
    return r.json()["conversation"]["status"] if r.status_code == 200 else None


async def wait_closed(sdk: dict[str, Any], conversation_id: str, tries: int = 40) -> bool:
    """无状态会话在响应发出之后才关闭，轮询一下避免和收尾事务抢跑。"""
    for _ in range(tries):
        if await conversation_status(sdk, conversation_id) == "closed":
            return True
        await asyncio.sleep(0.05)
    return False


@pytest.fixture
async def sdk(pg: dict[str, Any], redis_it: Any) -> AsyncIterator[dict[str, Any]]:
    app = build_app(pg, redis_it)
    key = await new_key(pg, redis_it, pg["app_a"])
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t", timeout=30
    )
    client = AsyncOpenAI(base_url="http://t/v1", api_key=key, http_client=http)
    try:
        yield {"client": client, "app_id": pg["app_a"], "key": key, "http": http}
    finally:
        await client.close()
        await app.state.chat_runtime.drain()


async def test_models_list_returns_the_key_app(sdk: dict[str, Any]) -> None:
    listed = await sdk["client"].models.list()

    assert [m.id for m in listed.data] == [str(sdk["app_id"])]
    assert listed.data[0].owned_by == "aics"


async def test_blocking_completion(sdk: dict[str, Any]) -> None:
    resp = await sdk["client"].chat.completions.create(
        model=str(sdk["app_id"]), messages=[{"role": "user", "content": QUESTION}]
    )

    assert resp.id.startswith("chatcmpl-")
    assert resp.object == "chat.completion"
    assert resp.model == str(sdk["app_id"])
    assert resp.choices[0].message.role == "assistant"
    assert resp.choices[0].message.content == ANSWER
    assert resp.choices[0].finish_reason == "stop"
    assert resp.usage is not None
    assert resp.usage.total_tokens == resp.usage.prompt_tokens + resp.usage.completion_tokens
    aics = extra(resp, "aics")
    assert aics is not None
    assert aics["reply_by"] == "ai"
    assert aics["answer_type"] == "answer"
    assert aics["citations"][0]["n"] == 1


async def test_streaming_completion(sdk: dict[str, Any]) -> None:
    stream = await sdk["client"].chat.completions.create(
        model=str(sdk["app_id"]),
        messages=[{"role": "user", "content": QUESTION}],
        stream=True,
    )

    parts: list[str] = []
    finishes: list[str] = []
    aics: Any = None
    chunks = 0
    async for chunk in stream:
        chunks += 1
        assert chunk.object == "chat.completion.chunk"
        choice = chunk.choices[0]
        if choice.delta.content:
            parts.append(choice.delta.content)
        if choice.finish_reason:
            finishes.append(choice.finish_reason)
        aics = extra(chunk, "aics") or aics

    assert chunks > 1
    assert "".join(parts) == ANSWER
    assert finishes == ["stop"]
    assert aics is not None
    assert aics["citations"][0]["n"] == 1


async def test_client_history_and_ignored_params(sdk: dict[str, Any]) -> None:
    """`messages` 里的历史被当作请求级历史；`tools` 与 `n>1` 按契约忽略。"""
    resp = await sdk["client"].chat.completions.create(
        model=str(sdk["app_id"]),
        messages=[
            {"role": "system", "content": "忽略我"},  # 客户端 system 忽略
            {"role": "user", "content": "退款多久到账？"},
            {"role": "assistant", "content": "退款 3–5 个工作日原路退回[1]。"},
            {"role": "user", "content": QUESTION},
        ],
        tools=TOOLS,
        n=2,
        temperature=0.9,  # 只收窄：应用默认 0.2，取 min
        max_tokens=50,
    )

    assert len(resp.choices) == 1
    assert resp.choices[0].message.content == ANSWER
    assert resp.usage is not None
    assert resp.usage.completion_tokens <= 50


async def test_unknown_model_is_404_model_not_found(sdk: dict[str, Any]) -> None:
    with pytest.raises(APIStatusError) as excinfo:
        await sdk["client"].chat.completions.create(
            model=str(uuid4()), messages=[{"role": "user", "content": QUESTION}]
        )

    assert excinfo.value.status_code == 404
    assert error_code(excinfo.value) == "model_not_found"


async def test_bad_key_is_401(sdk: dict[str, Any]) -> None:
    bogus = AsyncOpenAI(base_url="http://t/v1", api_key="aics_" + "x" * 32, http_client=sdk["http"])

    with pytest.raises(AuthenticationError) as excinfo:
        await bogus.models.list()

    assert excinfo.value.status_code == 401


async def test_stateless_requests_get_their_own_conversation(sdk: dict[str, Any]) -> None:
    """不带 `X-AICS-Conversation-Id` 时每次请求另开会话，历史只来自请求体。"""
    ids = []
    for _ in range(2):
        resp = await sdk["client"].chat.completions.create(
            model=str(sdk["app_id"]), messages=[{"role": "user", "content": QUESTION}]
        )
        aics = extra(resp, "aics")
        ids.append(aics["conversation_id"])
        assert aics["previous_conversation_id"] is None

    assert ids[0] != ids[1]
    assert await wait_closed(sdk, ids[0])


async def test_closed_conversation_starts_a_new_one(sdk: dict[str, Any]) -> None:
    """把已关闭（无状态收尾）的会话 id 传回来：服务端自动新建并告知上一个会话。"""
    first = await sdk["client"].chat.completions.create(
        model=str(sdk["app_id"]), messages=[{"role": "user", "content": QUESTION}]
    )
    closed_id = extra(first, "aics")["conversation_id"]
    assert await wait_closed(sdk, closed_id)

    resp = await sdk["client"].chat.completions.create(
        model=str(sdk["app_id"]),
        messages=[{"role": "user", "content": QUESTION}],
        extra_headers={"X-AICS-Conversation-Id": closed_id},
    )

    aics = extra(resp, "aics")
    assert aics["previous_conversation_id"] == closed_id
    assert aics["conversation_id"] != closed_id
    assert await conversation_status(sdk, aics["conversation_id"]) == "active"
