"""M2.6 ② turn execution without a database: run_rag events → queue, persistence hook,
error / interrupt handling, silent dispatches. (`_write_assistant` is stubbed; the PG
behaviour is covered by tests/integration/test_chat_service_pg.py.)"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import pytest

from aics.core.errors import AppError
from aics.memory.service import MemoryService
from aics.memory.store import InMemoryConversationStore
from aics.memory.units import in_memory_units
from aics.modules.apps.service import AppService
from aics.modules.chat import service as chat_service
from aics.modules.chat.runtime import ChatRuntime
from aics.modules.chat.service import (
    ChatService,
    ErrorEvent,
    StartEvent,
    TurnHandle,
    TurnRequest,
    TurnResult,
    _Prepared,
)
from aics.rag.ports import ChatTurn
from aics.rag.schemas import AppProfile, CitationsEvent, DeltaEvent
from tests.chat_support import KB, responder
from tests.rag_support import FakeKB, FakeLLMProvider, node_of

APP = uuid.uuid4()


def runtime(llm: FakeLLMProvider) -> ChatRuntime:
    kb = FakeKB(KB)
    return ChatRuntime(
        sessions=None,  # type: ignore[arg-type]
        chunk_search=kb,
        vector=kb,
        reranker=kb,
        llm=llm,
        memory=MemoryService(in_memory_units(InMemoryConversationStore())),
    )


def prepared(
    question: str, dispatch: str = "rag", *, req: dict[str, Any] | None = None, **kw: Any
) -> _Prepared:
    cfg = AppService(None).build_config(  # type: ignore[arg-type]
        rag_config=None, handoff_config=None, session_config=None, chunk_config=None
    )
    return _Prepared(
        req=TurnRequest(app_id=APP, question=question, **(req or {})),
        t0=time.perf_counter(),
        config=cfg,
        profile=AppProfile(name="商城", fallback_text="暂时无法回答"),
        custom_dict=[],
        config_version=1,
        conversation_id=uuid.uuid4(),
        previous_conversation_id=None,
        user_message_id=uuid.uuid4(),
        reply_id=uuid.uuid4(),
        dispatch=dispatch,  # type: ignore[arg-type]
        handoff_status=kw.pop("handoff_status", "none"),
        refusal_streak=0,
        last_assistant_was_clarify=False,
        **kw,
    )


class Recorder:
    def __init__(self) -> None:
        self.messages: list[Any] = []

    async def __call__(self, p: Any, msg: Any, *, intent: Any, handoff_reason: Any) -> str:
        self.messages.append(msg)
        return "pending" if msg.handoff else p.handoff_status


def service(llm: FakeLLMProvider, monkeypatch: pytest.MonkeyPatch) -> tuple[ChatService, Recorder]:
    svc = ChatService(runtime(llm))
    rec = Recorder()
    monkeypatch.setattr(svc, "_write_assistant", rec)
    return svc, rec


async def drain(h: TurnHandle) -> list[Any]:
    return [ev async for ev in h.events()]


async def test_answer_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, rec = service(FakeLLMProvider(responder), monkeypatch)
    h = TurnHandle(svc, prepared("退货要几天？"))
    evs = await drain(h)
    assert isinstance(evs[0], StartEvent) and isinstance(evs[1], CitationsEvent)
    assert isinstance(evs[-1], TurnResult)
    res = evs[-1]
    assert res.answer == "".join(e.text for e in evs if isinstance(e, DeltaEvent))
    assert res.reply_by == "ai" and res.used_citations == [1] and res.first_token_ms is not None
    (msg,) = rec.messages
    assert msg.answer_type == "answer" and msg.intent == "kb_qa" and msg.citations == res.citations
    assert "timings" in msg.retrieval_trace
    await asyncio.sleep(0)  # done-callbacks run on the next loop iteration
    assert not svc.runtime.inflight


async def test_handoff_flow_status(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, rec = service(FakeLLMProvider(responder), monkeypatch)
    res = await TurnHandle(svc, prepared("我要人工")).result()
    assert res.answer_type == "handoff" and res.handoff and res.handoff_status == "pending"
    assert res.handoff_reason == "keyword" and rec.messages[0].handoff


async def test_error_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(messages: Any, kwargs: dict[str, Any]) -> str:
        if node_of(messages) == "generate":
            raise RuntimeError("x")
        return responder(messages, kwargs)

    svc, rec = service(FakeLLMProvider(broken), monkeypatch)
    h = TurnHandle(svc, prepared("退货要几天？"))
    evs = await drain(h)
    assert isinstance(evs[-1], ErrorEvent) and evs[-1].status == 502
    assert rec.messages[0].answer_type == "error"
    assert rec.messages[0].decision_reason == "UPSTREAM_ERROR"
    with pytest.raises(AppError):
        await TurnHandle(svc, prepared("退货要几天？")).result()


async def test_interrupt_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, rec = service(FakeLLMProvider(responder, stream_delay_s=0.05), monkeypatch)
    h = TurnHandle(svc, prepared("退货要几天？"))
    async for ev in h.events():
        if isinstance(ev, DeltaEvent):
            h.cancel()
            h.cancel()  # idempotent
    await svc.runtime.drain()
    (msg,) = rec.messages
    assert msg.interrupted and msg.answer_type == "answer" and msg.content


async def test_cancel_after_completion_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, rec = service(FakeLLMProvider(responder), monkeypatch)
    h = TurnHandle(svc, prepared("退货要几天？"))
    res = await h.result()
    h.cancel()
    assert not rec.messages[0].interrupted and res.answer_type == "answer"


@pytest.mark.parametrize("dispatch,reply_by", [("wait", "system"), ("agent", "none")])
async def test_silent_dispatch(
    dispatch: str, reply_by: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc, rec = service(FakeLLMProvider(responder), monkeypatch)
    p = prepared(
        "在吗",
        dispatch,
        handoff_status="pending" if dispatch == "wait" else "accepted",
        system_reply="请稍候" if dispatch == "wait" else None,
    )
    h = TurnHandle(svc, p)
    evs = await drain(h)
    start, res = evs[0], evs[-1]
    assert isinstance(res, TurnResult) and res.reply_by == reply_by and res.answer_type is None
    if dispatch == "wait":
        assert start.message_id == p.reply_id and [e.text for e in evs[1:-1]] == ["请稍候"]
    else:
        assert start.message_id == p.user_message_id == res.message_id and len(evs) == 2
    assert rec.messages == []


async def test_next_event_timeout() -> None:
    svc = ChatService(runtime(FakeLLMProvider(responder, delay_s=0.3)))
    svc._write_assistant = Recorder()  # type: ignore[method-assign]
    h = TurnHandle(svc, prepared("退货要几天？"))
    assert isinstance(await h.next_event(1), StartEvent)
    with pytest.raises(TimeoutError):
        await h.next_event(0.01)
    await asyncio.wait_for(h.result(), 5)


# ---------------------------------------------------------------- 无状态轮次（M3.3 ②）
async def test_stateless_turn_closes_the_conversation(monkeypatch: pytest.MonkeyPatch) -> None:
    """无状态轮次结束即关闭本轮建出的会话（`close_reason=stateless`），三条出口都收口。"""
    svc, _ = service(FakeLLMProvider(responder), monkeypatch)
    closed: list[tuple[Any, str]] = []

    class Sessions:
        def __init__(self) -> None:
            self.commits = 0

        def __call__(self) -> Sessions:
            return self

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        async def commit(self) -> None:
            self.commits += 1

    class Repo:
        def __init__(self, session: Any) -> None:
            self.session = session

        async def close(self, app_id: Any, cid: Any, *, reason: str, at: Any = None) -> bool:
            closed.append((cid, reason))
            return True

    monkeypatch.setattr(chat_service, "ConversationRepository", Repo)
    sessions = Sessions()
    monkeypatch.setattr(svc.runtime, "sessions", sessions)
    p = prepared("退货要几天？", stateless=True)
    await drain(TurnHandle(svc, p))
    assert closed == [(p.conversation_id, "stateless")] and sessions.commits == 1


async def test_stateless_turn_skips_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    """无状态轮次既不写事实也不压缩：会话随即关闭，没有可压缩的服务端历史。"""
    svc, _ = service(FakeLLMProvider(responder), monkeypatch)
    calls: list[str] = []

    async def capture(*a: Any, **k: Any) -> list[Any]:
        calls.append("capture_slots")
        return []

    async def compact(*a: Any, **k: Any) -> tuple[str, Any]:
        calls.append("maybe_compact")
        return ("none", None)

    monkeypatch.setattr(svc.runtime.memory, "capture_slots", capture)
    monkeypatch.setattr(svc.runtime.memory, "maybe_compact", compact)
    await drain(TurnHandle(svc, prepared("退货要几天？", stateless=True)))
    assert calls == []
    await drain(TurnHandle(svc, prepared("退货要几天？")))
    assert calls == ["capture_slots", "maybe_compact"]


def test_rag_overrides_apply_to_the_current_turn_only() -> None:
    """请求级参数只覆盖本轮的配置副本，绝不改应用配置（M3.3 OpenAI 参数取 min）。"""
    svc = ChatService(runtime(FakeLLMProvider(responder)))
    p = prepared("退货要几天？", req={"rag_overrides": {"rerank_top_n": 1, "max_tokens": 100}})
    cfg = svc._rag_input(p).config
    assert cfg.rerank_top_n == 1 and cfg.max_tokens == 100
    assert p.config.rag.rerank_top_n != 1 and p.config.rag.max_tokens != 100


def test_inline_history_reaches_the_rag_input() -> None:
    """请求内历史一路传到 RagInput，由 memory 按无状态路径组装。"""
    svc = ChatService(runtime(FakeLLMProvider(responder)))
    hist = [ChatTurn(role="user", content="上一句"), ChatTurn(role="assistant", content="上一答")]
    p = prepared("退货要几天？", req={"inline_history": hist})
    assert svc._rag_input(p).inline_history == hist
