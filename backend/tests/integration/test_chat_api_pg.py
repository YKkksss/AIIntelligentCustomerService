"""M2.6 ③ (integration): chat HTTP API over real PG + Redis with fake models.

Blocking vs SSE consistency, SSE event order per branch, mid-stream disconnect (real uvicorn
socket) → interrupted, closed conversation → new one, concurrent requests on one conversation,
app isolation, validation, upstream errors, message listing with cursor pagination."""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from sqlalchemy import select, update
from tests.chat_support import make_runtime, responder
from tests.rag_support import FakeLLMProvider, node_of

from aics.core.config import get_settings
from aics.db.models import Conversation, Message
from aics.main import create_app
from aics.modules.apikeys.service import ApiKeyAuthenticator, ApiKeyService
from aics.modules.apps.service import AppService
from aics.modules.chat.repository import ConversationRepository

pytestmark = pytest.mark.integration


async def new_key(pg: dict[str, Any], redis: Any, app_id: UUID, scopes: list[str]) -> str:
    pepper = get_settings().api_key_pepper.get_secret_value()
    async with pg["sessions"]() as s:
        _, plain = await ApiKeyService(s, pepper=pepper, redis=redis).create(
            app_id, name="api-it", scopes=scopes, rpm_limit=1000
        )
        await s.commit()
    return plain


def build_app(pg: dict[str, Any], redis: Any, llm: FakeLLMProvider | None = None) -> FastAPI:
    app = create_app()
    app.state.authenticator = ApiKeyAuthenticator(
        pg["sessions"], pepper=get_settings().api_key_pepper.get_secret_value(), redis=redis
    )
    app.state.chat_runtime = make_runtime(pg["sessions"], llm=llm)
    return app


@pytest.fixture
async def api(pg: dict[str, Any], redis_it: Any) -> AsyncIterator[dict[str, Any]]:
    app = build_app(pg, redis_it)
    ka = await new_key(pg, redis_it, pg["app_a"], ["chat"])
    kb = await new_key(pg, redis_it, pg["app_b"], ["chat"])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t", timeout=30) as c:
        yield {"client": c, "app": app, "ka": ka, "kb": kb, **pg, "redis": redis_it}
    await app.state.chat_runtime.drain()


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def parse_sse(text: str) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    for block in text.split("\n\n"):
        lines = [x for x in block.split("\n") if x]
        if not lines or all(x.startswith(":") for x in lines):
            continue
        ev = next(x[7:] for x in lines if x.startswith("event: "))
        data = json.loads(next(x[6:] for x in lines if x.startswith("data: ")))
        out.append((ev, data))
    return out


async def post_chat(api: dict[str, Any], app_id: UUID, key: str, **body: Any) -> httpx.Response:
    return await api["client"].post(f"/api/v1/apps/{app_id}/chat", json=body, headers=auth(key))


async def test_blocking_and_sse_agree(api: dict[str, Any]) -> None:
    a, k = api["app_a"], api["ka"]
    r = await post_chat(api, a, k, question="退货要几天？", user_id="u1")
    assert r.status_code == 200, r.text
    blk = r.json()
    assert r.headers["X-RateLimit-Limit"] == "1000"
    assert blk["reply_by"] == "ai" and blk["answer_type"] == "answer" and blk["handoff"] is False
    assert blk["previous_conversation_id"] is None and blk["handoff_status"] == "none"
    assert blk["intent"] == {"name": "kb_qa", "confidence": 0.9, "source": "llm"}
    assert [c["n"] for c in blk["citations"]] == [1] and blk["usage"]["total_tokens"] > 0
    s = await post_chat(api, a, k, question="退货要几天？", stream=True)
    assert s.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(s.text)
    names = [e for e, _ in events]
    assert names[0] == "start" and names[1] == "citations" and names[-1] == "end"
    assert set(names[2:-1]) == {"delta"}
    start, end = events[0][1], events[-1][1]
    text = "".join(d["text"] for e, d in events if e == "delta")
    assert text == blk["answer"] == end["answer"]
    assert end["answer_type"] == blk["answer_type"] and end["intent"] == blk["intent"]
    assert end["used_citations"] == [c["n"] for c in blk["citations"]]
    assert end["reply_by"] == "ai" and end["handoff_status"] == "none"
    cand = events[1][1]["citations"]
    assert {c["n"] for c in cand} >= set(end["used_citations"])
    rows = await messages(api, a, UUID(start["conversation_id"]))
    assert rows[-1].id == UUID(start["message_id"]) and rows[-1].content == text


async def messages(api: dict[str, Any], app_id: UUID, cid: UUID) -> list[Message]:
    async with api["sessions"]() as s:
        rows = await s.execute(
            select(Message)
            .where(Message.app_id == app_id, Message.conversation_id == cid)
            .order_by(Message.created_at, Message.id)
        )
        return list(rows.scalars())


async def sse_names(api: dict[str, Any], app_id: UUID, key: str, **body: Any) -> list[Any]:
    r = await post_chat(api, app_id, key, stream=True, **body)
    assert r.status_code == 200, r.text
    return parse_sse(r.text)


async def test_sse_order_per_branch(api: dict[str, Any]) -> None:
    a, k = api["app_a"], api["ka"]
    refused = await sse_names(api, a, k, question="公司股票代码多少？")
    assert [e for e, _ in refused] == ["start", "delta", "end"]
    assert refused[-1][1]["answer_type"] == "refused" and refused[-1][1]["used_citations"] == []
    chit = await sse_names(api, a, k, question="你好")
    assert [e for e, _ in chit] == ["start", "delta", "end"]
    assert chit[-1][1]["intent"]["name"] == "chitchat"
    ho = await sse_names(api, a, k, question="我要人工")
    assert [e for e, _ in ho] == ["start", "delta", "end"]
    end = ho[-1][1]
    assert end["answer_type"] == "handoff" and end["handoff"] and end["handoff_status"] == "pending"
    cid = UUID(ho[0][1]["conversation_id"])
    # wait mode → system reply; accepted → start then end
    async with api["sessions"]() as s:
        await AppService(s).update_app(a, {"handoff_config": {"handoff_ai_mode": "wait"}})
        await s.commit()
    wait = await sse_names(api, a, k, question="还要多久", conversation_id=str(cid))
    assert [e for e, _ in wait] == ["start", "delta", "end"]
    assert wait[-1][1]["reply_by"] == "system" and wait[-1][1]["answer_type"] is None
    async with api["sessions"]() as s:
        await s.execute(
            update(Conversation).where(Conversation.id == cid).values(handoff_status="accepted")
        )
        await s.commit()
    acc = await sse_names(api, a, k, question="在吗", conversation_id=str(cid))
    assert [e for e, _ in acc] == ["start", "end"]
    assert acc[-1][1]["reply_by"] == "none" and acc[-1][1]["answer"] is None
    rows = await messages(api, a, cid)
    assert acc[0][1]["message_id"] == str(rows[-1].id) and rows[-1].role == "user"
    blk = (await post_chat(api, a, k, question="在吗？", conversation_id=str(cid))).json()
    assert blk["reply_by"] == "none" and blk["answer"] is None and blk["answer_type"] is None


async def test_closed_conversation_auto_new(api: dict[str, Any]) -> None:
    a, k = api["app_a"], api["ka"]
    first = (await post_chat(api, a, k, question="退货要几天？")).json()
    async with api["sessions"]() as s:
        await ConversationRepository(s).close(a, UUID(first["conversation_id"]), reason="idle")
        await s.commit()
    r = (
        await post_chat(api, a, k, question="退款呢？", conversation_id=first["conversation_id"])
    ).json()
    assert r["conversation_id"] != first["conversation_id"]
    assert r["previous_conversation_id"] == first["conversation_id"]
    ev = await sse_names(api, a, k, question="退款呢？", conversation_id=first["conversation_id"])
    assert ev[-1][1]["previous_conversation_id"] == first["conversation_id"]


async def test_isolation_and_auth(api: dict[str, Any]) -> None:
    a, b, ka, kb = api["app_a"], api["app_b"], api["ka"], api["kb"]
    conv = (await post_chat(api, a, ka, question="退货要几天？")).json()["conversation_id"]
    r = await post_chat(api, b, ka, question="退货要几天？")  # A key on B path
    assert r.status_code == 404 and r.json()["error"]["code"] == "APP_NOT_FOUND"
    r = await post_chat(api, b, kb, question="继续", conversation_id=conv)  # B app, A's conv
    assert r.status_code == 404 and r.json()["error"]["code"] == "CONVERSATION_NOT_FOUND"
    r = await post_chat(api, b, kb, question="继续", conversation_id=conv, stream=True)
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/json")
    c = api["client"]
    r = await c.get(f"/api/v1/apps/{b}/conversations/{conv}/messages", headers=auth(kb))
    assert r.status_code == 404
    assert (await c.post(f"/api/v1/apps/{a}/chat", json={"question": "x"})).status_code == 401
    kr = await new_key(api, api["redis"], a, ["kb:read"])
    assert (await post_chat(api, a, kr, question="x")).json()["error"]["code"] == "FORBIDDEN"
    assert len(await messages(api, a, UUID(conv))) == 2


@pytest.mark.parametrize(
    "body,field",
    [
        ({"question": ""}, "question"),
        ({"question": "   "}, None),
        ({"question": "x" * 2001}, "question"),
        ({"question": "ok", "user_id": "u" * 129}, "user_id"),
        ({"question": "ok", "metadata": {"k": "v" * 4100}}, None),
        ({"question": "ok", "conversation_id": "not-a-uuid"}, "conversation_id"),
        ({"question": "ok", "stream": "maybe"}, "stream"),
    ],
)
async def test_validation(api: dict[str, Any], body: dict[str, Any], field: str | None) -> None:
    r = await post_chat(api, api["app_a"], api["ka"], **body)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    if field:
        assert any(field in e["loc"] for e in err["details"]["errors"])


async def test_question_at_limit_accepted(api: dict[str, Any]) -> None:
    r = await post_chat(api, api["app_a"], api["ka"], question="退" * 2000, metadata={"ch": "app"})
    assert r.status_code == 200
    async with api["sessions"]() as s:
        conv = await ConversationRepository(s).get_or_404(
            api["app_a"], UUID(r.json()["conversation_id"])
        )
    assert conv.metadata_ == {"ch": "app"}


async def test_concurrent_requests_same_conversation(api: dict[str, Any]) -> None:
    a, k = api["app_a"], api["ka"]
    cid = (await post_chat(api, a, k, question="退货要几天？")).json()["conversation_id"]
    rs = await asyncio.gather(
        post_chat(api, a, k, question="退款多久？", conversation_id=cid),
        post_chat(api, a, k, question="退货呢？", conversation_id=cid, stream=True),
        post_chat(api, a, k, question="还有吗？", conversation_id=cid),
    )
    assert [r.status_code for r in rs] == [200, 200, 200]
    rows = await messages(api, a, UUID(cid))
    assert len(rows) == 8 and [m.role for m in rows].count("assistant") == 4
    async with api["sessions"]() as s:
        conv = await ConversationRepository(s).get_or_404(a, UUID(cid))
    assert conv.message_count == 8


async def test_upstream_error_blocking_and_sse(pg: dict[str, Any], redis_it: Any) -> None:
    def broken(messages: Any, kwargs: dict[str, Any]) -> str:
        if node_of(messages) == "generate":
            raise RuntimeError("down")
        return responder(messages, kwargs)

    app = build_app(pg, redis_it, FakeLLMProvider(broken))
    key = await new_key(pg, redis_it, pg["app_a"], ["chat"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        url = f"/api/v1/apps/{pg['app_a']}/chat"
        r = await c.post(url, json={"question": "退货要几天？"}, headers=auth(key))
        assert r.status_code == 502 and r.json()["error"]["code"] == "UPSTREAM_ERROR"
        assert r.json()["error"]["request_id"]
        r = await c.post(url, json={"question": "退货要几天？", "stream": True}, headers=auth(key))
        ev = parse_sse(r.text)
        assert [e for e, _ in ev][0] == "start" and ev[-1][0] == "error"
        assert ev[-1][1]["code"] == "UPSTREAM_ERROR" and ev[-1][1]["request_id"]
        assert "citations" in [e for e, _ in ev]  # judged answerable before generation failed
    await app.state.chat_runtime.drain()


async def test_list_messages_cursor(api: dict[str, Any]) -> None:
    a, k = api["app_a"], api["ka"]
    cid = (await post_chat(api, a, k, question="退货要几天？")).json()["conversation_id"]
    for q in ("退款多久？", "公司股票代码？"):
        await post_chat(api, a, k, question=q, conversation_id=cid)
    c = api["client"]
    url = f"/api/v1/apps/{a}/conversations/{cid}/messages"
    p1 = (await c.get(url, params={"limit": 4}, headers=auth(k))).json()
    assert [m["role"] for m in p1["items"]] == ["user", "assistant", "user", "assistant"]
    assert p1["items"][-1]["answer_type"] == "refused" and p1["next_cursor"]
    assert p1["conversation"]["id"] == cid and p1["conversation"]["status"] == "active"
    assert p1["items"][1]["intent"]["name"] == "kb_qa" and p1["items"][0]["intent"] is None
    p2 = (
        await c.get(url, params={"limit": 4, "cursor": p1["next_cursor"]}, headers=auth(k))
    ).json()
    assert [m["content"] for m in p2["items"]][0] == "退货要几天？" and p2["next_cursor"] is None
    assert len(p2["items"]) == 2
    bad = await c.get(url, params={"cursor": "zzz"}, headers=auth(k))
    assert bad.status_code == 422
    assert (await c.get(url, params={"limit": 101}, headers=auth(k))).status_code == 422
    kh = await new_key(api, api["redis"], a, ["handoff:write"])
    assert (await c.get(url, headers=auth(kh))).status_code == 200


# ---------------------------------------------------------------- real socket: disconnect
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def test_sse_disconnect_mid_stream_marks_interrupted(
    pg: dict[str, Any], redis_it: Any
) -> None:
    llm = FakeLLMProvider(responder, stream_delay_s=0.15)
    app = build_app(pg, redis_it, llm)
    key = await new_key(pg, redis_it, pg["app_a"], ["chat"])
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, lifespan="off")
    )
    task = asyncio.create_task(server.serve())
    for _ in range(250):
        if server.started:
            break
        await asyncio.sleep(0.02)
    start: dict[str, Any] = {}
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10) as c:
            body = {"question": "退货要几天？", "stream": True}
            url = f"/api/v1/apps/{pg['app_a']}/chat"
            async with c.stream("POST", url, json=body, headers=auth(key)) as r:
                seen = ""
                async for chunk in r.aiter_text():
                    seen += chunk
                    if "event: start" in seen and not start:
                        line = seen.split("event: start\ndata: ", 1)[1].split("\n", 1)[0]
                        start = json.loads(line)
                    if "event: delta" in seen:
                        break  # close the connection after the first token
        for _ in range(100):  # the turn task persists after noticing the disconnect
            await asyncio.sleep(0.05)
            async with pg["sessions"]() as s:
                row = await s.scalar(select(Message).where(Message.id == UUID(start["message_id"])))
            if row is not None:
                break
        assert row is not None and row.interrupted and row.answer_type == "answer"
        full = "签收后 7 天内可以无理由退货[1]。"
        assert full.startswith(row.content) and len(row.content) < len(full)
    finally:
        server.should_exit = True
        await task
        await app.state.chat_runtime.drain()
