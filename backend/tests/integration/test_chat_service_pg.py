"""M2.6 ② (integration): chat.service.handle_turn over real PG with fake models.

Persistence order, assistant fields, refusal streak → handoff → pending, continue / wait /
accepted dispatch, closed → new conversation, 404 / isolation, upstream error, client
disconnect (interrupted) and concurrent turns on one conversation."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select, update
from tests.chat_support import make_runtime, responder
from tests.rag_support import FakeLLMProvider, node_of

from aics.core.errors import AppError
from aics.db.models import Conversation, Message
from aics.memory.units import pg_units
from aics.modules.apps.service import AppService
from aics.modules.chat.repository import ConversationRepository
from aics.modules.chat.service import ChatService, DeltaEvent, TurnRequest, TurnResult

pytestmark = pytest.mark.integration


async def messages_of(pg: dict[str, Any], app_id: UUID, cid: UUID) -> list[Message]:
    async with pg["sessions"]() as s:
        rows = await s.execute(
            select(Message)
            .where(Message.app_id == app_id, Message.conversation_id == cid)
            .order_by(Message.created_at, Message.id)
        )
        return list(rows.scalars())


async def conv_of(pg: dict[str, Any], app_id: UUID, cid: UUID) -> Conversation:
    async with pg["sessions"]() as s:
        return await ConversationRepository(s).get_or_404(app_id, cid)


async def patch_app(pg: dict[str, Any], app_id: UUID, patch: dict[str, Any]) -> None:
    async with pg["sessions"]() as s:
        await AppService(s).update_app(app_id, patch)
        await s.commit()


def ask(app_id: UUID, q: str, cid: UUID | None = None, **kw: Any) -> TurnRequest:
    return TurnRequest(app_id=app_id, question=q, conversation_id=cid, **kw)


async def test_answer_turn_persists_everything(pg: dict[str, Any]) -> None:
    a = pg["app_a"]
    rt = make_runtime(pg["sessions"])
    svc = ChatService(rt)
    res = await svc.handle_turn(ask(a, "订单 2026101400123 退货要几天？", external_user_id="u1"))
    assert res.reply_by == "ai" and res.answer_type == "answer" and res.handoff is False
    assert res.answer.startswith("签收后 7 天内") and res.used_citations == [1]
    c = res.citations[0]
    assert set(c) == {
        "n", "chunk_id", "document_id", "document_title", "heading_path", "snippet", "score", "page"
    }  # fmt: skip
    assert res.intent == {"name": "kb_qa", "confidence": 0.9, "source": "llm"}
    assert res.first_token_ms is not None and res.latency_ms >= res.first_token_ms
    user, bot = await messages_of(pg, a, res.conversation_id)
    assert (user.role, user.content, user.id) == (
        "user",
        "订单 2026101400123 退货要几天？",
        res.user_message_id,
    )
    assert bot.id == res.message_id and bot.answer_type == "answer" and bot.intent == "kb_qa"
    assert bot.intent_source == "llm" and bot.citations == res.citations
    assert bot.rewritten_query and bot.prompt_version and bot.config_version == 1
    assert bot.retrieval_trace and "timings" in bot.retrieval_trace
    assert bot.prompt_tokens and bot.first_token_ms == res.first_token_ms
    conv = await conv_of(pg, a, res.conversation_id)
    assert conv.external_user_id == "u1" and conv.channel == "api" and conv.message_count == 2
    # capture_slots: the order number is a fact right after the turn
    async with pg_units(pg["sessions"])() as u:
        facts = {f.key: f.value for f in await u.store.list_facts(a, res.conversation_id)}
    assert facts.get("order_no") == "2026101400123"


async def test_user_message_exists_before_generation(pg: dict[str, Any]) -> None:
    a = pg["app_a"]
    llm = FakeLLMProvider(responder, delay_s=0.3)
    svc = ChatService(make_runtime(pg["sessions"], llm=llm))
    h = await svc.start_turn(ask(a, "退货要几天？"))
    rows = await messages_of(pg, a, h.conversation_id)
    assert [m.role for m in rows] == ["user"]  # generation still running
    final = await h.result()
    assert [m.role for m in await messages_of(pg, a, final.conversation_id)] == [
        "user",
        "assistant",
    ]


async def test_refusals_then_rule_handoff_then_dispatch(pg: dict[str, Any]) -> None:
    a = pg["app_a"]
    svc = ChatService(make_runtime(pg["sessions"]))
    r1 = await svc.handle_turn(ask(a, "公司股票代码多少？"))
    assert r1.answer_type == "refused"
    cid = r1.conversation_id
    assert (await conv_of(pg, a, cid)).refusal_streak == 1
    r2 = await svc.handle_turn(ask(a, "那市值呢？", cid))
    assert r2.answer_type == "refused" and (await conv_of(pg, a, cid)).refusal_streak == 2
    r3 = await svc.handle_turn(ask(a, "随便问问", cid))  # streak ≥ max_refusals → rule handoff
    assert r3.answer_type == "handoff" and r3.handoff and r3.handoff_reason == "max_refusals"
    conv = await conv_of(pg, a, cid)
    assert conv.handoff_status == "pending" and conv.handoff_reason == "max_refusals"
    assert conv.refusal_streak == 0 and conv.handoff_requested_at is not None
    assert r3.handoff_status == "pending"
    # pending + continue: AI keeps answering
    r4 = await svc.handle_turn(ask(a, "退款多久到账？", cid))
    assert r4.reply_by == "ai" and r4.answer_type == "answer" and r4.handoff_status == "pending"
    # pending + wait: system wait text, no rag
    await patch_app(pg, a, {"handoff_config": {"handoff_ai_mode": "wait"}})
    r5 = await svc.handle_turn(ask(a, "还要等多久？", cid))
    assert r5.reply_by == "system" and r5.answer_type is None
    assert r5.answer == "已为您转接人工客服，请稍候。"
    rows = await messages_of(pg, a, cid)
    assert (rows[-1].role, rows[-1].kind, rows[-1].id) == ("system", "handoff_wait", r5.message_id)
    # accepted: no reply at all; message_id is the user message
    async with pg["sessions"]() as s:
        await s.execute(
            update(Conversation).where(Conversation.id == cid).values(handoff_status="accepted")
        )
        await s.commit()
    r6 = await svc.handle_turn(ask(a, "你好？", cid))
    assert r6.reply_by == "none" and r6.answer is None and r6.message_id == r6.user_message_id
    rows = await messages_of(pg, a, cid)
    assert rows[-1].role == "user" and rows[-1].id == r6.message_id
    assert (await conv_of(pg, a, cid)).refusal_streak == 0


async def test_keyword_handoff_once(pg: dict[str, Any]) -> None:
    a = pg["app_a"]
    svc = ChatService(make_runtime(pg["sessions"]))
    r = await svc.handle_turn(ask(a, "我要人工"))
    assert r.answer_type == "handoff" and r.handoff_reason == "keyword"
    t0 = (await conv_of(pg, a, r.conversation_id)).handoff_requested_at
    r2 = await svc.handle_turn(ask(a, "快点转人工", r.conversation_id))
    assert r2.handoff  # this turn triggered it again, but the state does not change
    conv = await conv_of(pg, a, r.conversation_id)
    assert conv.handoff_status == "pending" and conv.handoff_requested_at == t0


async def test_closed_conversation_starts_a_new_one(pg: dict[str, Any]) -> None:
    a = pg["app_a"]
    svc = ChatService(make_runtime(pg["sessions"]))
    r1 = await svc.handle_turn(ask(a, "订单 2026101400999 退货要几天？", external_user_id="u9"))
    async with pg["sessions"]() as s:
        await ConversationRepository(s).close(a, r1.conversation_id, reason="idle")
        await s.commit()
    r2 = await svc.handle_turn(ask(a, "退款多久到账？", r1.conversation_id))
    assert r2.conversation_id != r1.conversation_id
    assert r2.previous_conversation_id == r1.conversation_id
    conv = await conv_of(pg, a, r2.conversation_id)
    assert conv.previous_conversation_id == r1.conversation_id and conv.external_user_id == "u9"
    assert conv.message_count == 2
    async with pg_units(pg["sessions"])() as u:  # facts are not carried over
        assert await u.store.list_facts(a, r2.conversation_id) == []


async def test_not_found_and_isolation(pg: dict[str, Any]) -> None:
    a, b = pg["app_a"], pg["app_b"]
    svc = ChatService(make_runtime(pg["sessions"]))
    r = await svc.handle_turn(ask(a, "退货要几天？"))
    for app_id, cid in ((b, r.conversation_id), (a, UUID(int=1))):
        with pytest.raises(AppError) as ei:
            await svc.start_turn(ask(app_id, "退款呢？", cid))
        assert ei.value.code == "CONVERSATION_NOT_FOUND"
    assert len(await messages_of(pg, a, r.conversation_id)) == 2  # nothing written by B


async def test_upstream_error_persists_error_message(pg: dict[str, Any]) -> None:
    a = pg["app_a"]

    def broken(messages: Any, kwargs: dict[str, Any]) -> str:
        if node_of(messages) == "generate":
            raise RuntimeError("provider exploded")
        return responder(messages, kwargs)

    svc = ChatService(make_runtime(pg["sessions"], llm=FakeLLMProvider(broken)))
    h = await svc.start_turn(ask(a, "退货要几天？"))
    with pytest.raises(AppError) as ei:
        await h.result()
    assert (ei.value.code, ei.value.status) == ("UPSTREAM_ERROR", 502)
    rows = await messages_of(pg, a, h.conversation_id)
    assert [m.role for m in rows] == ["user", "assistant"]
    assert rows[1].answer_type == "error" and rows[1].decision_reason == "UPSTREAM_ERROR"
    assert (await conv_of(pg, a, h.conversation_id)).refusal_streak == 0


async def test_client_disconnect_persists_interrupted(pg: dict[str, Any]) -> None:
    a = pg["app_a"]
    llm = FakeLLMProvider(responder, stream_delay_s=0.05)
    rt = make_runtime(pg["sessions"], llm=llm)
    svc = ChatService(rt)
    r0 = await svc.handle_turn(ask(a, "公司股票代码多少？"))  # streak 1
    h = await svc.start_turn(ask(a, "退货要几天？", r0.conversation_id))
    got = ""
    async for ev in h.events():
        if isinstance(ev, DeltaEvent):
            got += ev.text
            if len(got) >= 4:
                h.cancel()  # the client went away mid-answer
                break
    await rt.drain()
    rows = await messages_of(pg, a, r0.conversation_id)
    bot = rows[-1]
    assert bot.role == "assistant" and bot.interrupted and bot.answer_type == "answer"
    assert bot.content and "签收后 7 天内可以无理由退货[1]。".startswith(bot.content)
    assert len(bot.content) < len("签收后 7 天内可以无理由退货[1]。")
    assert (await conv_of(pg, a, r0.conversation_id)).refusal_streak == 1  # unchanged


async def test_concurrent_turns_on_one_conversation(pg: dict[str, Any]) -> None:
    a = pg["app_a"]
    svc = ChatService(make_runtime(pg["sessions"], llm=FakeLLMProvider(responder, delay_s=0.05)))
    r = await svc.handle_turn(ask(a, "退货要几天？"))
    results = await asyncio.gather(
        *(
            svc.handle_turn(ask(a, q, r.conversation_id))
            for q in ("退款多久？", "退货呢？", "股票？")
        )
    )
    assert all(isinstance(x, TurnResult) for x in results)
    rows = await messages_of(pg, a, r.conversation_id)
    assert len(rows) == 8 and sum(m.role == "assistant" for m in rows) == 4
    conv = await conv_of(pg, a, r.conversation_id)
    assert conv.message_count == 8 and conv.refusal_streak in (0, 1)


async def test_clarify_flag_and_chitchat_rule(pg: dict[str, Any]) -> None:
    a = pg["app_a"]
    seen: list[str] = []

    def spy(messages: Any, kwargs: dict[str, Any]) -> str:
        seen.append(node_of(messages))
        return responder(messages, kwargs)

    svc = ChatService(make_runtime(pg["sessions"], llm=FakeLLMProvider(spy)))
    r = await svc.handle_turn(ask(a, "你好"))
    assert r.intent == {"name": "chitchat", "confidence": 1.0, "source": "rule"} and seen == []
    r2 = await svc.handle_turn(ask(a, "那个怎么弄", r.conversation_id))
    assert r2.intent["name"] == "clarify" and r2.answer == "您想问退货还是退款？"
    rows = await messages_of(pg, a, r.conversation_id)
    assert rows[-1].intent == "clarify" and rows[-1].citations == []


async def test_maybe_compact_enqueues_online(pg: dict[str, Any]) -> None:
    a = pg["app_a"]
    await patch_app(
        pg,
        a,
        {
            "rag_config": {
                "history_token_budget": 500,
                "compact_trigger_tokens": 500,
                "keep_recent_turns": 1,
            }
        },
    )
    rt = make_runtime(pg["sessions"])
    svc = ChatService(rt)
    long_q = "退货要几天？" + "我想详细说明一下情况，" * 60
    r = await svc.handle_turn(ask(a, long_q))
    for _ in range(2):
        await svc.handle_turn(ask(a, long_q, r.conversation_id))
    assert rt.enqueued, "online turns must enqueue compaction once uncovered > trigger"
    app_id, cid, version, params = rt.enqueued[0]
    assert (app_id, cid, version) == (a, r.conversation_id, 0)
    assert params.keep_recent_turns == 1 and params.compact_trigger_tokens == 500


async def test_native_turn_without_conversation_id_stays_active(pg: dict[str, Any]) -> None:
    """M3.3 回归：原生接口不带 `conversation_id` 是「新建持久会话」，不是无状态轮次。

    阶段 ② 曾用 `conversation_id is None` 推断无状态，导致原生新建的会话被当场关闭
    （`close_reason=stateless`）、跳过记忆写入，第二轮只能新建空会话，recall 读不到事实。
    """
    a = pg["app_a"]
    svc = ChatService(make_runtime(pg["sessions"]))
    res = await svc.handle_turn(ask(a, "退货要几天？"))
    conv = await conv_of(pg, a, res.conversation_id)
    assert conv.status == "active" and conv.close_reason is None
    assert conv.message_count == 2


async def test_recall_answers_from_captured_facts(pg: dict[str, Any]) -> None:
    """M2.5 known limit: asking back for info given earlier answers from facts (0007)."""
    a = pg["app_a"]
    svc = ChatService(make_runtime(pg["sessions"]))
    first = await svc.handle_turn(ask(a, "订单 2026101400123 退货要几天？"))
    res = await svc.handle_turn(ask(a, "我刚才给你的订单号是多少？", first.conversation_id))
    assert res.answer_type == "answer" and res.handoff is False
    assert res.intent == {"name": "recall", "confidence": 0.95, "source": "llm"}
    assert "2026101400123" in res.answer and res.citations == []
    bot = (await messages_of(pg, a, res.conversation_id))[-1]
    assert bot.intent == "recall" and bot.decision_reason == "intent:recall"
    conv = await conv_of(pg, a, res.conversation_id)
    assert conv.refusal_streak == 0 and conv.handoff_status == "none"
