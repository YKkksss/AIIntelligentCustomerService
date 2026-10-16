"""M2.3: the RAG graph end to end with a FakeLLM and an in-memory knowledge base.

Acceptance (开发计划 M2.3): 20 sample questions — answerable ones answer with citations,
unanswerable ones are refused; judge failure counts as unanswerable.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest
from langchain_core.messages import BaseMessage

from aics.rag.config import AppRagConfig, HandoffConfig
from aics.rag.graph import run_rag, run_rag_collect
from aics.rag.llm import LlmCallError, LlmUnavailableError, reset_breakers
from aics.rag.ports import ChatTurn, ContextBundle, ContextRequest, RagPorts
from aics.rag.schemas import (
    AppProfile,
    CitationsEvent,
    DeltaEvent,
    RagInput,
    RagResult,
    RunMode,
    TraceEvent,
)
from tests.rag_support import APP, FakeKB, FakeLLMProvider, KbChunk, classify_json, node_of, tag

FALLBACK = "抱歉，这个问题我暂时无法回答。"
PROFILE = AppProfile(name="小象商城", description="小象商城售前售后咨询", fallback_text=FALLBACK)


def cid(n: int) -> uuid.UUID:
    return uuid.UUID(int=0xC000 + n)


CHUNKS = [
    KbChunk(cid(1), "售后政策", "退货 > 时限", "签收后 7 天内可申请无理由退货。", ("退货",)),
    KbChunk(
        cid(2), "售后政策", "退款 > 到账", "退款原路返回，1-3 个工作日到账。", ("退款", "到账")
    ),
    KbChunk(cid(3), "售后政策", "换货 > 运费", "质量问题换货运费由商家承担。", ("换货", "运费")),
    KbChunk(cid(4), "物流说明", "发货", "下单后 48 小时内发货，节假日顺延。", ("发货",)),
    KbChunk(
        cid(5), "物流说明", "配送范围", "支持全国配送，港澳台及海外暂不配送。", ("配送", "海外")
    ),
    KbChunk(cid(6), "会员说明", "积分", "每消费 1 元累计 1 积分，积分可抵现。", ("积分",)),
    KbChunk(cid(7), "会员说明", "会员等级", "年消费满 2000 元升级为金卡会员。", ("会员", "金卡")),
    KbChunk(cid(8), "发票说明", "开票", "订单完成后可在订单详情申请电子发票。", ("发票",)),
]

# (question, expected: answer | refuse, rerank top score, llm judge verdict for gray / no-score)
SAMPLES: list[tuple[str, str, float | None, bool | None]] = [
    ("退货期限是多久？", "answer", 0.92, None),
    ("退款几天到账", "answer", 0.95, None),
    ("换货运费谁出？", "answer", 0.88, None),
    ("你们多久发货", "answer", 0.9, None),
    ("能配送到海外吗", "answer", 0.85, None),
    ("积分怎么算", "answer", 0.93, None),
    ("怎么成为金卡会员", "answer", 0.9, None),
    ("发票在哪里开", "answer", 0.91, None),
    ("退货需要什么条件", "answer", 0.7, True),  # gray zone → LLM yes
    ("会员积分能抵现吗", "answer", 0.6, True),  # gray zone → LLM yes
    ("节假日发货吗", "answer", 0.82, None),
    ("退款原路返回吗", "answer", 0.86, None),
    ("你们公司的上市代码是多少", "refuse", None, None),  # no candidates match → low scores
    ("店铺老板叫什么名字", "refuse", None, None),
    ("退货可以上门取件吗", "refuse", 0.3, None),  # below threshold
    ("发货用什么快递公司", "refuse", 0.45, None),  # below threshold
    ("积分会过期吗", "refuse", 0.65, False),  # gray zone → LLM no
    ("金卡会员有生日礼吗", "refuse", 0.7, False),  # gray zone → LLM no
    ("海外仓库在哪里", "refuse", 0.4, None),
    ("换货要多久", "refuse", 0.55, False),
]


def make_responder(
    *, judge: dict[str, bool] | None = None, generate: str | None = None, classify: Any = None
) -> Any:
    judge = judge or {}

    def respond(messages: list[BaseMessage], kwargs: dict[str, Any]) -> str:
        node = node_of(messages)
        text = str(messages[0].content)
        if node == "classify":
            q = tag(text, "question")
            return classify(q) if callable(classify) else classify_json(q)
        if node == "judge":
            q = tag(text, "question")
            return json.dumps({"answerable": judge.get(q, False), "reason": "test"})
        if node == "condense":
            return "退款几天到账"
        if node == "chitchat":
            return "您好呀，有业务问题随时问我～"
        if generate is not None:
            return generate
        ctx = tag(text, "context")
        first = ctx.split("\n")[1] if "\n" in ctx else ctx
        return f"根据资料：{first}[1]"

    return respond


def kb_with(scores: dict[str, float]) -> FakeKB:
    kb = FakeKB(CHUNKS)
    kb.rerank_override = scores
    return kb


def inp(question: str, **kw: Any) -> RagInput:
    return RagInput(app_id=APP, question=question, profile=PROFILE, **kw)


@pytest.fixture(autouse=True)
def _fresh_breakers() -> None:
    reset_breakers()


def ports(kb: FakeKB, llm: FakeLLMProvider, **kw: Any) -> RagPorts:
    return RagPorts(chunk_search=kb, vector=kb, reranker=kb, llm=llm, **kw)


# ---------------------------------------------------------------- acceptance: 20 samples
@pytest.mark.parametrize(("question", "expected", "score", "verdict"), SAMPLES)
async def test_twenty_samples(
    question: str, expected: str, score: float | None, verdict: bool | None
) -> None:
    kb = kb_with({question: score} if score is not None else {})
    llm = FakeLLMProvider(make_responder(judge={question: bool(verdict)}))
    res, events = await run_rag_collect(inp(question), ports(kb, llm))
    assert isinstance(res, RagResult)
    if expected == "answer":
        assert res.answer_type == "answer", res.decision_reason
        assert res.citations, "answerable questions must cite"
        assert "[1]" in res.answer
        assert res.citations[0].index == 1
        assert any(isinstance(e, CitationsEvent) for e in events)
    else:
        assert res.answer_type == "refused", res.decision_reason
        assert res.answer == FALLBACK
        assert not res.citations
        assert not any(isinstance(e, CitationsEvent) for e in events)
        assert "generate" not in llm.nodes()
    assert res.intent.name == "kb_qa" and res.intent.source == "llm"
    assert res.prompt_versions["condense_classify"] == "1"
    if verdict is not None:
        assert "judge" in llm.nodes()
    elif score is not None:
        assert "judge" not in llm.nodes()  # outside the gray zone no LLM judge


async def test_sample_split() -> None:
    assert len(SAMPLES) == 20
    assert sum(1 for s in SAMPLES if s[1] == "answer") == 12


# ---------------------------------------------------------------- judge failure
@pytest.mark.parametrize("mode", ["bad_json", "error", "timeout"])
async def test_judge_failure_is_unanswerable(mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    q = "退货需要什么条件"
    base = make_responder()

    def respond(messages: list[BaseMessage], kwargs: dict[str, Any]) -> str:
        if node_of(messages) == "judge":
            if mode == "bad_json":
                return "我觉得可以回答"
            raise RuntimeError("judge upstream 500")
        return base(messages, kwargs)

    llm = FakeLLMProvider(respond)
    if mode == "timeout":
        monkeypatch.setattr("aics.rag.nodes.answer.JUDGE_TIMEOUT_S", 0.01)
        llm.delay_s = 0.05
        cfg = AppRagConfig(intent={"enabled": False})  # skip classify so only judge is slow
    else:
        cfg = AppRagConfig()
    res, _ = await run_rag_collect(inp(q, config=cfg), ports(kb_with({q: 0.7}), llm))
    assert res.answer_type == "refused"
    assert res.decision_reason == "judge_failed"
    assert "judge_failed" in res.degraded
    assert res.trace["judge"]["answerable"] is False


async def test_judge_failure_with_on_unanswerable_hands_off() -> None:
    q = "退货需要什么条件"

    def respond(messages: list[BaseMessage], kwargs: dict[str, Any]) -> str:
        if node_of(messages) == "judge":
            return "not json"
        return make_responder()(messages, kwargs)

    res, _ = await run_rag_collect(
        inp(q, handoff=HandoffConfig(on_unanswerable=True)),
        ports(kb_with({q: 0.7}), FakeLLMProvider(respond)),
    )
    assert res.answer_type == "handoff"
    assert res.handoff_reason == "unanswerable"
    assert res.decision_reason == "judge_failed"


async def test_rerank_failure_uses_llm_judge() -> None:
    q = "退货期限是多久？"
    kb = kb_with({})
    kb.fail_rerank = True
    llm = FakeLLMProvider(make_responder(judge={q: True}))
    res, _ = await run_rag_collect(inp(q), ports(kb, llm))
    assert "rerank_failed" in res.degraded
    assert "judge" in llm.nodes()
    assert res.answer_type == "answer" and res.decision_reason == "judge_llm_yes"


async def test_rerank_disabled_uses_llm_judge_without_degraded() -> None:
    q = "积分怎么算"
    llm = FakeLLMProvider(make_responder(judge={q: False}))
    res, _ = await run_rag_collect(
        inp(q, config=AppRagConfig(enable_rerank=False)), ports(kb_with({}), llm)
    )
    assert res.answer_type == "refused" and res.decision_reason == "judge_llm_no"
    assert "rerank_failed" not in res.degraded


async def test_gray_zone_without_llm_judge_is_answerable() -> None:
    q = "退货需要什么条件"
    llm = FakeLLMProvider(make_responder())
    res, _ = await run_rag_collect(
        inp(q, config=AppRagConfig(judge_with_llm=False)), ports(kb_with({q: 0.6}), llm)
    )
    assert res.answer_type == "answer" and res.decision_reason == "gray_answerable"
    assert "judge" not in llm.nodes()


# ---------------------------------------------------------------- streaming
async def test_event_order_debug() -> None:
    q = "退款几天到账"
    llm = FakeLLMProvider(make_responder())
    events = [e async for e in run_rag(inp(q, run_mode=RunMode.debug), ports(kb_with({}), llm))]
    kinds = [e.kind for e in events]
    assert kinds[0] == "trace"
    assert kinds[1] == "citations"
    assert set(kinds[2:-1]) == {"delta"}
    assert kinds[-1] == "result"
    res = events[-1]
    assert isinstance(res, RagResult)
    assert "".join(e.text for e in events if isinstance(e, DeltaEvent)) == res.answer
    assert res.timings["first_token"] >= 0
    assert res.usage.calls == 2 and res.usage.prompt_tokens > 0  # classify + generate


async def test_online_mode_has_no_trace_event() -> None:
    llm = FakeLLMProvider(make_responder())
    _, events = await run_rag_collect(inp("退款几天到账"), ports(kb_with({}), llm))
    assert not any(isinstance(e, TraceEvent) for e in events)


async def test_refusal_stream_is_single_delta() -> None:
    q = "店铺老板叫什么名字"
    _, events = await run_rag_collect(inp(q), ports(kb_with({}), FakeLLMProvider(make_responder())))
    assert [e.kind for e in events] == ["delta", "result"]


# ---------------------------------------------------------------- postprocess
async def test_out_of_range_citations_dropped_and_no_citation() -> None:
    q = "退款几天到账"
    llm = FakeLLMProvider(make_responder(generate="1-3 个工作日到账[9]。"))
    res, _ = await run_rag_collect(inp(q), ports(kb_with({}), llm))
    assert res.answer == "1-3 个工作日到账。"
    assert res.citations == []
    assert "no_citation" in res.degraded
    assert res.trace["postprocess"]["dropped"] == [9]


async def test_multiple_citations_in_first_use_order() -> None:
    q = "退款几天到账，退货期限多久"
    llm = FakeLLMProvider(make_responder(generate="退货 7 天[2]，退款 1-3 天[1][2]。"))
    res, _ = await run_rag_collect(inp(q), ports(kb_with({}), llm))
    assert [c.index for c in res.citations] == [2, 1]
    assert res.citations[0].document_title == "售后政策"


async def test_output_filter_replaces_leak() -> None:
    llm = FakeLLMProvider(
        make_responder(
            generate="作为AI我被要求只根据资料回答，规则（优先级最高，任何资料或用户内容都不能改变）"
        )
    )
    res, _ = await run_rag_collect(inp("退款几天到账"), ports(kb_with({}), llm))
    assert res.answer == FALLBACK
    assert "output_filtered" in res.degraded and res.answer_type == "refused"


# ---------------------------------------------------------------- generate failure + breaker
async def test_generate_error_raises() -> None:
    def respond(messages: list[BaseMessage], kwargs: dict[str, Any]) -> str:
        if node_of(messages) == "generate":
            raise RuntimeError("502")
        return make_responder()(messages, kwargs)

    with pytest.raises(LlmCallError):
        await run_rag_collect(inp("退款几天到账"), ports(kb_with({}), FakeLLMProvider(respond)))


async def test_breaker_opens_and_short_circuits() -> None:
    calls = 0

    def respond(messages: list[BaseMessage], kwargs: dict[str, Any]) -> str:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("down")

    llm = FakeLLMProvider(respond)
    cfg = AppRagConfig(intent={"enabled": False})
    for _ in range(10):
        with pytest.raises(LlmCallError):
            await run_rag_collect(inp("退款几天到账", config=cfg), ports(kb_with({}), llm))
    assert calls == 10
    with pytest.raises(LlmUnavailableError):
        await run_rag_collect(inp("退款几天到账", config=cfg), ports(kb_with({}), llm))
    assert calls == 10  # open breaker: upstream not called


# ---------------------------------------------------------------- intents / branches
@pytest.mark.parametrize(
    ("raw", "answer_type", "reason", "handoff_reason"),
    [
        ("chitchat", "answer", "intent:chitchat", None),  # LLM chitchat reply
        ("out_of_scope", "refused", "intent:out_of_scope", None),
        ("human", "handoff", "intent:human", "intent_human"),
        ("action", "handoff", "intent:action", "intent_action"),
    ],
)
async def test_intent_branches(
    raw: str, answer_type: str, reason: str, handoff_reason: str | None
) -> None:
    llm = FakeLLMProvider(make_responder(classify=lambda q: classify_json(q, raw, 0.95)))
    res, events = await run_rag_collect(inp("今天心情不错哈哈"), ports(kb_with({}), llm))
    assert (res.answer_type, res.decision_reason, res.handoff_reason) == (
        answer_type,
        reason,
        handoff_reason,
    )
    assert res.intent.name == raw and res.intent.source == "llm"
    assert res.citations == [] and "generate" not in llm.nodes()
    assert llm.nodes()[0] == "classify"
    assert not any(isinstance(e, CitationsEvent) for e in events)


async def test_clarify_branch_and_no_double_clarify() -> None:
    def cl(q: str) -> str:
        return classify_json(
            q, "clarify", 0.8, need_clarify=True, clarify_question="请问是退货还是换货？"
        )

    llm = FakeLLMProvider(make_responder(classify=cl))
    res, _ = await run_rag_collect(inp("那个怎么弄"), ports(kb_with({}), llm))
    assert res.answer == "请问是退货还是换货？" and res.intent.name == "clarify"
    res2, _ = await run_rag_collect(
        inp("那个怎么弄", last_assistant_was_clarify=True), ports(kb_with({}), llm)
    )
    assert res2.intent.name == "kb_qa" and res2.trace["intent"]["fallback_reason"] == (
        "clarify_rejected"
    )


@pytest.mark.parametrize(
    ("classify", "reason"),
    [
        (lambda q: "not json at all", "parse_error"),
        (lambda q: classify_json(q, "chitchat", 0.3), "low_confidence"),
        (lambda q: classify_json(q, "chitchat", 0.9), "chitchat_too_long"),
    ],
)
async def test_classify_fallbacks_go_to_kb_qa(classify: Any, reason: str) -> None:
    q = "你好，请问退款原路返回吗，大概几个工作日能到账呢"
    llm = FakeLLMProvider(make_responder(classify=classify))
    res, _ = await run_rag_collect(inp(q), ports(kb_with({}), llm))
    assert res.intent.name == "kb_qa" and res.intent.source == "fallback"
    assert res.trace["intent"]["fallback_reason"] == reason
    assert res.answer_type == "answer"


async def test_classify_timeout_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = FakeLLMProvider(make_responder(), delay_s=0.7)
    cfg = AppRagConfig(intent={"classify_timeout_ms": 500}, enable_rerank=True)
    res, _ = await run_rag_collect(inp("退款几天到账", config=cfg), ports(kb_with({}), llm))
    assert res.trace["intent"]["fallback_reason"] == "timeout"
    assert "classify_failed" in res.degraded
    assert res.rewritten_query == "退款几天到账"


async def test_handoff_disabled_keeps_kb_qa() -> None:
    llm = FakeLLMProvider(make_responder(classify=lambda q: classify_json(q, "human", 0.95)))
    res, _ = await run_rag_collect(
        inp("退款几天到账", handoff=HandoffConfig(enabled=False)), ports(kb_with({}), llm)
    )
    assert res.intent.name == "kb_qa" and res.answer_type == "answer"


async def test_intent_disabled_without_history_skips_llm() -> None:
    llm = FakeLLMProvider(make_responder())
    res, _ = await run_rag_collect(
        inp("退款几天到账", config=AppRagConfig(intent={"enabled": False})),
        ports(kb_with({}), llm),
    )
    assert llm.nodes() == ["generate"]
    assert res.intent.source == "fallback" and res.prompt_version == "gen1"


async def test_condense_with_inline_history() -> None:
    llm = FakeLLMProvider(make_responder())
    hist = [ChatTurn(role="user", content="退款政策"), ChatTurn(role="assistant", content="...")]
    res, _ = await run_rag_collect(
        inp("几天到账", config=AppRagConfig(intent={"enabled": False}), inline_history=hist),
        ports(kb_with({}), llm),
    )
    assert llm.nodes()[0] == "condense"
    assert res.rewritten_query == "退款几天到账"
    assert res.prompt_version == "cd1.gen1"


async def test_prompt_version_string_and_model() -> None:
    q = "退货需要什么条件"
    llm = FakeLLMProvider(make_responder(judge={q: True}))
    res, _ = await run_rag_collect(inp(q), ports(kb_with({q: 0.7}), llm))
    assert res.prompt_version == "cc1.judge1.gen1"
    assert res.llm_model == "fake-chat"


# ---------------------------------------------------------------- context
async def test_memory_port_context_used_and_failure_degrades() -> None:
    seen: list[ContextRequest] = []

    class Mem:
        async def load_context(self, req: ContextRequest) -> ContextBundle:
            seen.append(req)
            return ContextBundle(
                history=[ChatTurn(role="user", content="我想退款")],
                facts={"order_no": "2026101400123"},
                usage={"history": 5},
            )

    llm = FakeLLMProvider(make_responder())
    res, _ = await run_rag_collect(inp("几天到账"), ports(kb_with({}), llm, memory=Mem()))
    assert seen and seen[0].budget == 3000
    assert res.context_usage == {"history": 5}
    cls = next(c for c in llm.calls if c["node"] == "classify")
    assert "[user] 我想退款" in str(cls["messages"][0].content)
    assert "order_no=2026101400123" in str(cls["messages"][0].content)

    class Broken:
        async def load_context(self, req: ContextRequest) -> ContextBundle:
            raise RuntimeError("pg down")

    hist = [ChatTurn(role="user", content=f"q{i}") for i in range(20)]
    res2, _ = await run_rag_collect(
        inp("几天到账", inline_history=hist), ports(kb_with({}), llm, memory=Broken())
    )
    assert "context_degraded" in res2.degraded


async def test_classify_call_parameters() -> None:
    llm = FakeLLMProvider(make_responder())
    await run_rag_collect(inp("退款几天到账"), ports(kb_with({}), llm))
    cls = next(c for c in llm.calls if c["node"] == "classify")
    assert cls["kwargs"]["response_format"] == {"type": "json_object"}
    assert llm.overrides[0]["temperature"] == 0.0 and llm.overrides[0]["max_tokens"] == 200
    gen = llm.overrides[-1]
    assert gen["streaming"] is True and gen["max_tokens"] == 800 and gen["temperature"] == 0.2


async def test_missing_llm_port_rejected() -> None:
    kb = kb_with({})
    with pytest.raises(ValueError):
        await run_rag_collect(inp("x"), RagPorts(chunk_search=kb, vector=kb))


async def test_cancellation_propagates() -> None:
    llm = FakeLLMProvider(make_responder(), delay_s=5)
    task = asyncio.create_task(run_rag_collect(inp("退款几天到账"), ports(kb_with({}), llm)))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
