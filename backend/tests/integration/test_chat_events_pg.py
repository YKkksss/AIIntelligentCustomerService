"""M3.5 ② 集成：会话事件落库与事件流（SSE）在真 PG + 真 Redis 上的行为（ADR-0016）。

分两层：
- 事件本身：真跑一轮对话/坐席动作，断言 `conversation_events` 的 `seq`、类型与 payload，
  以及配了 Webhook 的应用是否顺手排了 `webhook_deliveries`；
- 事件流：会话已关闭时走真 HTTP（ASGI 直连）断言补发帧与 `Last-Event-ID`；实时推送用真 Redis
  的 pub/sub（httpx 的 ASGITransport 会把响应读到底，长连接只能直接消费生成器）。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select, update
from tests.chat_support import make_runtime

from aics.core.config import get_settings
from aics.db.models import App, Conversation, ConversationEvent, WebhookDelivery
from aics.main import create_app
from aics.modules.apikeys.service import ApiKeyAuthenticator, ApiKeyService
from aics.modules.chat import event_stream, events
from aics.modules.chat.repository import ConversationRepository
from aics.modules.chat.schemas import NewConversation
from aics.modules.chat.service import ChatService, TurnRequest
from aics.modules.handoff.service import HandoffService

pytestmark = pytest.mark.integration
M = timedelta(minutes=1)

#: 一句就命中转人工关键词（`handoff.keywords` 默认含「人工」）
ASK_HUMAN = "我要人工"


async def new_key(pg: dict[str, Any], redis: Any, app_id: UUID, scopes: list[str]) -> str:
    pepper = get_settings().api_key_pepper.get_secret_value()
    async with pg["sessions"]() as s:
        _, plain = await ApiKeyService(s, pepper=pepper, redis=redis).create(
            app_id, name="events-it", scopes=scopes, rpm_limit=1000
        )
        await s.commit()
    return plain


async def new_conv(pg: dict[str, Any], app_id: UUID, **conv: Any) -> UUID:
    async with pg["sessions"]() as s:
        c = await ConversationRepository(s).create(
            app_id, NewConversation(channel="api", external_user_id="u1")
        )
        if conv:
            await s.execute(update(Conversation).where(Conversation.id == c.id).values(**conv))
        await s.commit()
        return c.id


async def events_of(pg: dict[str, Any], cid: UUID) -> list[ConversationEvent]:
    async with pg["sessions"]() as s:
        rows = await s.execute(
            select(ConversationEvent)
            .where(ConversationEvent.conversation_id == cid)
            .order_by(ConversationEvent.seq)
        )
        return list(rows.scalars())


async def deliveries_of(pg: dict[str, Any], cid: UUID) -> list[WebhookDelivery]:
    async with pg["sessions"]() as s:
        rows = await s.execute(
            select(WebhookDelivery)
            .where(WebhookDelivery.conversation_id == cid)
            .order_by(WebhookDelivery.seq)
        )
        return list(rows.scalars())


async def set_webhook(pg: dict[str, Any], app_id: UUID) -> None:
    """直接摆好 `webhook_config`：密钥由 `POST …/webhook/secret` 轮换（M4.5），这里只写库。"""
    async with pg["sessions"]() as s:
        await s.execute(
            update(App)
            .where(App.id == app_id)
            .values(
                webhook_config={
                    "url": "https://receiver.example/aics",
                    "secret_enc": "gAAAAA-fake",
                    "secret_prefix": "whsec_abcd",
                }
            )
        )
        await s.commit()


@pytest.fixture
async def http(pg: dict[str, Any], redis_it: Any) -> AsyncIterator[dict[str, Any]]:
    """真 HTTP（ASGI 直连）+ 假模型；事件流走补发路径（会话结束即断开）。"""
    app = create_app()
    app.state.authenticator = ApiKeyAuthenticator(
        pg["sessions"], pepper=get_settings().api_key_pepper.get_secret_value(), redis=redis_it
    )
    app.state.chat_runtime = make_runtime(pg["sessions"])
    key = await new_key(pg, redis_it, pg["app_a"], ["chat"])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t", timeout=30) as c:
        yield {"client": c, "key": key, **pg}
    await app.state.chat_runtime.drain()


def sse_frames(body: str) -> list[dict[str, Any]]:
    """把 SSE 文本拆成 `(id, data)`：`id:` 行给序号，`data:` 行给 payload。"""
    frames: list[dict[str, Any]] = []
    for block in body.split("\n\n"):
        if not block.startswith("id: "):
            continue
        head, data = block.split("\ndata: ", 1)
        frames.append({"id": int(head[len("id: ") :]), "payload": json.loads(data)})
    return frames


# --------------------------------------------------------------------------- 事件落库


async def test_handoff_turn_writes_events_in_the_same_transaction(pg: dict[str, Any]) -> None:
    """触发转人工那一轮：只有 `handoff.requested`（§7.3：普通 AI 问答不进事件流）。"""
    a = pg["app_a"]
    svc = ChatService(make_runtime(pg["sessions"]))
    first = await svc.handle_turn(TurnRequest(app_id=a, question=ASK_HUMAN))
    rows = await events_of(pg, first.conversation_id)
    assert [r.seq for r in rows] == [1]
    row = rows[0]
    assert row.type == "handoff.requested" and row.app_id == a
    assert row.data["reason"] == "keyword"
    assert row.data["handoff_ai_mode"] == "continue"
    # §3.16：`recent_messages` 是「转人工那一刻」的最近 10 条（旧→新），含本轮 AI 的答复
    assert [m["role"] for m in row.data["recent_messages"]] == ["user", "assistant"]

    # 排队期间的第二轮：用户与系统/坐席消息都发 message.created，会话仍是同一条
    second = await svc.handle_turn(
        TurnRequest(app_id=a, question="那要等多久？", conversation_id=first.conversation_id)
    )
    rows = await events_of(pg, first.conversation_id)
    assert [r.type for r in rows] == ["handoff.requested", "message.created", "message.created"]
    assert [r.seq for r in rows] == [1, 2, 3]
    assert second.conversation_id == first.conversation_id


async def test_deliveries_are_queued_for_apps_with_a_webhook(pg: dict[str, Any]) -> None:
    """配了地址与密钥的应用：每个可投递事件排一条 `pending`，`id` 就是事件 id（§3.16）。"""
    a = pg["app_a"]
    await set_webhook(pg, a)
    svc = ChatService(make_runtime(pg["sessions"]))
    turn = await svc.handle_turn(TurnRequest(app_id=a, question=ASK_HUMAN))
    rows = await deliveries_of(pg, turn.conversation_id)
    assert [d.event for d in rows] == ["handoff.requested"]
    d = rows[0]
    assert d.id == d.event_id
    assert (d.status, d.attempts, d.last_status_code, d.delivered_at) == ("pending", 0, None, None)
    assert d.next_attempt_at is not None
    assert d.payload["id"] == str(d.id) and d.payload["seq"] == d.seq
    assert d.payload["conversation_id"] == str(turn.conversation_id)


# --------------------------------------------------------------------------- 事件流（HTTP）


async def test_stream_replays_the_backlog_and_ends_on_closed(
    http: dict[str, Any],
) -> None:
    a = http["app_a"]
    cid = await new_conv(
        http,
        a,
        status="closed",
        closed_at=datetime.now(UTC),
        close_reason="idle",
        channel="api",
        external_user_id="u_10086",
    )
    async with http["sessions"]() as s:
        conv = await ConversationRepository(s).get_or_404(a, cid)
        target = events.Target.of(conv)
        await events.emit(s, target, "message.created", {"message": {"id": "m1"}})
        await events.emit(s, target, "conversation.closed", {"reason": "idle"})
        await s.commit()

    resp = await http["client"].get(
        f"/api/v1/apps/{a}/conversations/{cid}/events",
        headers={"Authorization": f"Bearer {http['key']}"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    frames = sse_frames(resp.text)
    assert [f["id"] for f in frames] == [1, 2]
    assert [f["payload"]["event"] for f in frames] == ["message.created", "conversation.closed"]
    # `data` 与出站 Webhook 的 payload 同一形状（同一个构造器）
    first = frames[0]["payload"]
    assert set(first) == {
        "id",
        "event",
        "app_id",
        "conversation_id",
        "user_id",
        "seq",
        "occurred_at",
        "data",
    }
    assert first["user_id"] == "u_10086" and first["app_id"] == str(a)


async def test_last_event_id_replays_only_newer_events(http: dict[str, Any]) -> None:
    a = http["app_a"]
    cid = await new_conv(http, a, status="closed", close_reason="idle", closed_at=datetime.now(UTC))
    async with http["sessions"]() as s:
        conv = await ConversationRepository(s).get_or_404(a, cid)
        target = events.Target.of(conv)
        for i in range(3):
            await events.emit(s, target, "message.created", {"message": {"id": f"m{i}"}})
        await events.emit(s, target, "conversation.closed", {"reason": "idle"})
        await s.commit()

    resp = await http["client"].get(
        f"/api/v1/apps/{a}/conversations/{cid}/events",
        headers={"Authorization": f"Bearer {http['key']}", "Last-Event-ID": "2"},
    )
    frames = sse_frames(resp.text)
    assert [f["id"] for f in frames] == [3, 4]
    assert frames[-1]["payload"]["event"] == "conversation.closed"


async def test_invalid_last_event_id_is_rejected(http: dict[str, Any]) -> None:
    a = http["app_a"]
    cid = await new_conv(http, a)
    resp = await http["client"].get(
        f"/api/v1/apps/{a}/conversations/{cid}/events",
        headers={"Authorization": f"Bearer {http['key']}", "Last-Event-ID": "abc"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


# --------------------------------------------------------------------------- 事件流（实时）


async def test_stream_pushes_live_events_from_redis(pg: dict[str, Any], redis_it: Any) -> None:
    """真 pub/sub：坐席接上后，挂在流上的客户端在心跳周期内收到新事件（ADR-0016 §4）。"""
    a = pg["app_a"]
    cid = await new_conv(
        pg,
        a,
        handoff_status="pending",
        handoff_requested_at=datetime.now(UTC),
        channel="api",
        external_user_id="u1",
    )
    settings = get_settings()
    publisher = events.RedisPublisher(redis_it, prefix=settings.redis_prefix)
    frames: list[str] = []

    async def consume() -> None:
        async for chunk in event_stream.stream(
            sessions=pg["sessions"],
            redis=redis_it,
            prefix=settings.redis_prefix,
            app_id=a,
            conversation_id=cid,
            last_seq=0,
            heartbeat_s=5.0,
        ):
            if not chunk.startswith("id: "):
                continue  # 订阅确认后的第一次 get_message 会空转一拍 → `: ping`
            frames.append(chunk)
            return  # 收到第一条事件就够：证明频道通知把新事件推了过来

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.3)  # 让 SUBSCRIBE 先落地（ADR-0016：先订阅再补发）
    async with pg["sessions"]() as s:
        await HandoffService(s, publisher=publisher).accept(
            a, cid, agent_id="ag_1", agent_name="小艾"
        )
    await asyncio.wait_for(task, timeout=15)

    assert frames, "实时事件没有推到流上"
    ids = [int(f.split("\n", 1)[0][len("id: ") :]) for f in frames]
    assert ids == [1]  # 默认没有 accepted_text，接入只发 handoff.accepted 一条
    payload = json.loads(frames[0].split("data: ", 1)[1])
    assert payload["event"] == "handoff.accepted"
    assert payload["data"]["agent_id"] == "ag_1"
    assert payload["data"]["agent_name"] == "小艾"
