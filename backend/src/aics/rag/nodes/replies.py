"""Non-kb_qa branches and handoff (意图识别 §6.3; 会话生命周期与转人工 §3).

None of these emit citations or go through postprocess.
- chitchat_reply: rule hit → template by category; LLM-classified chitchat → short LLM reply
  (≤ 60 字, max_tokens 100, no thinking), falling back to the template on any failure;
- blocked_reply: sensitive keyword → `intent.sensitive_reply` (refused);
- out_of_scope_reply / clarify_reply: fixed reply / the clarify question;
- handoff: rule (keyword / max_refusals), intent (human / action) or judge (on_unanswerable);
- recall_reply (M2.6, 设计讨论 0007): the user asks about information they gave earlier in this
  conversation → short LLM reply grounded only in facts / summary / history (no knowledge-base
  retrieval, no citations); empty memory → fixed "not found" reply without an LLM call; LLM
  failure → a deterministic list of the stored facts. The identifier guardrail checks the
  answer against facts / question / history.
"""

from __future__ import annotations

import time
from typing import Any

import structlog
from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime

from aics.core import metrics
from aics.core.errors import AppError
from aics.core.masking import mask_identifier
from aics.rag import prompting
from aics.rag.identifiers import check_identifiers
from aics.rag.intent.templates import chitchat_category
from aics.rag.intent.templates import chitchat_reply as template_reply
from aics.rag.llm import StreamState
from aics.rag.nodes.answer import emit_trace
from aics.rag.schemas import DeltaEvent
from aics.rag.state import RagDeps, RagState, merged

log = structlog.get_logger()

CHITCHAT_MAX_TOKENS = 100
CHITCHAT_MAX_CHARS = 120  # final answer cap (prompt asks for ≤ 60 字; max_tokens bounds it)
RECALL_MAX_TOKENS = 200
RECALL_MAX_CHARS = 200  # prompt asks for ≤ 80 字
RECALL_NOT_FOUND = "抱歉，我在本次对话记录里没有找到您说的信息，麻烦您再告诉我一次。"
FACT_LABELS = {
    "order_no": "订单号",
    "phone": "手机号",
    "product": "商品",
    "address_region": "地区",
    "issue_type": "问题类型",
}


def _reply(
    state: RagState,
    runtime: Runtime[RagDeps],
    text: str,
    answer_type: str,
    reason: str,
    handoff_reason: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    emit_trace(state, runtime)
    runtime.stream_writer(DeltaEvent(text=text))
    return merged(
        state,
        answer=text,
        answer_type=answer_type,
        decision_reason=reason,
        handoff_reason=handoff_reason,
        citations=[],
        **extra,
    )


def _template(state: RagState) -> str:
    inp = state["input"]
    resolved = state["intent"]
    category = (resolved.rule_hit or {}).get("category") or chitchat_category(inp.question)
    return template_reply(category, inp.config.intent, greeting=inp.profile.greeting)


async def chitchat_reply(state: RagState, runtime: Runtime[RagDeps]) -> dict[str, Any]:
    if state["intent"].source == "rule":
        return _reply(state, runtime, _template(state), "answer", "intent:chitchat(rule)")
    inp = state["input"]
    prompt = prompting.load("chitchat")
    text = prompting.render(
        prompt,
        app_name=inp.profile.name,
        system_prompt=(inp.profile.system_prompt or "").strip(),
        language=prompting.LANGUAGES.get(inp.profile.language, inp.profile.language),
        question=" ".join(inp.question.split()),
    )
    emit_trace(state, runtime)
    st = StreamState()
    t0 = time.perf_counter()
    try:
        async for piece in runtime.context.llm.stream(
            [HumanMessage(text)],
            st,
            node="chitchat",
            model=inp.config.llm_model,
            temperature=0.7,
            max_tokens=CHITCHAT_MAX_TOKENS,
            thinking=False,
        ):
            runtime.stream_writer(DeltaEvent(text=piece))
    except (TimeoutError, AppError) as e:
        if st.text:  # partial reply already streamed; keep it
            log.warning("rag.chitchat_interrupted", error_type=type(e).__name__)
        else:
            log.warning("rag.chitchat_failed", error_type=type(e).__name__)
            fallback = _template(state)
            runtime.stream_writer(DeltaEvent(text=fallback))
            return merged(
                state,
                answer=fallback,
                answer_type="answer",
                decision_reason="intent:chitchat",
                citations=[],
                degraded=["chitchat_llm_failed"],
                prompt_versions={"chitchat": str(prompt.version)},
            )
    answer = st.text[:CHITCHAT_MAX_CHARS].strip() or _template(state)
    return merged(
        state,
        answer=answer,
        answer_type="answer",
        decision_reason="intent:chitchat",
        citations=[],
        usage=st.usage,
        prompt_versions={"chitchat": str(prompt.version)},
        timings={"chitchat": int((time.perf_counter() - t0) * 1000)},
    )


async def blocked_reply(state: RagState, runtime: Runtime[RagDeps]) -> dict[str, Any]:
    text = state["input"].config.intent.sensitive_reply
    return _reply(state, runtime, text, "refused", "sensitive_keyword")


def out_of_scope_text(state: RagState) -> str:
    inp = state["input"]
    if inp.config.intent.out_of_scope_reply:
        return inp.config.intent.out_of_scope_reply
    return f"抱歉，我是{inp.profile.name}的客服助手，只能回答与本业务相关的问题。"


async def out_of_scope_reply(state: RagState, runtime: Runtime[RagDeps]) -> dict[str, Any]:
    return _reply(state, runtime, out_of_scope_text(state), "refused", "intent:out_of_scope")


async def clarify_reply(state: RagState, runtime: Runtime[RagDeps]) -> dict[str, Any]:
    q = state["intent"].clarify_question or ""
    return _reply(state, runtime, q, "answer", "intent:clarify")


async def handoff(state: RagState, runtime: Runtime[RagDeps]) -> dict[str, Any]:
    inp = state["input"]
    resolved = state["intent"]
    if state.get("decision") == "handoff":  # judge: unanswerable + on_unanswerable
        return _reply(
            state, runtime, inp.handoff.text, "handoff", state["decision_reason"], "unanswerable"
        )
    if resolved.source == "rule":
        rule = (resolved.rule_hit or {}).get("rule")
        reason = "max_refusals" if rule == "max_refusals" else "keyword"
        return _reply(state, runtime, inp.handoff.text, "handoff", f"rule:{reason}", reason)
    if resolved.intent == "action":
        return _reply(
            state,
            runtime,
            inp.config.intent.action_reply,
            "handoff",
            "intent:action",
            "intent_action",
        )
    return _reply(state, runtime, inp.handoff.text, "handoff", "intent:human", "intent_human")


def _facts_reply(facts: dict[str, str]) -> str:
    shown = [(FACT_LABELS[k], v) for k, v in facts.items() if k in FACT_LABELS and v]
    if not shown:
        return RECALL_NOT_FOUND
    return "根据本次对话记录：" + "；".join(f"{k}：{v}" for k, v in shown) + "。"


async def recall_reply(state: RagState, runtime: Runtime[RagDeps]) -> dict[str, Any]:
    inp = state["input"]
    facts = dict(state.get("facts") or {})
    summary = state.get("summary")
    history = state.get("history") or []
    if not (facts or summary or history):
        return _reply(state, runtime, RECALL_NOT_FOUND, "answer", "intent:recall(empty)")
    prompt = prompting.load("recall")
    text = prompting.render(
        prompt,
        app_name=inp.profile.name,
        language=prompting.LANGUAGES.get(inp.profile.language, inp.profile.language),
        facts=prompting.format_facts(facts),
        summary=prompting.format_summary(summary),
        history=prompting.format_history(history),
        question=" ".join(inp.question.split()),
    )
    emit_trace(state, runtime)
    st = StreamState()
    t0 = time.perf_counter()
    versions = {"recall": str(prompt.version)}
    degraded: list[str] = []
    try:
        async for piece in runtime.context.llm.stream(
            [HumanMessage(text)],
            st,
            node="recall",
            model=inp.config.llm_model,
            temperature=0.0,
            max_tokens=RECALL_MAX_TOKENS,
            thinking=False,
        ):
            runtime.stream_writer(DeltaEvent(text=piece))
    except (TimeoutError, AppError) as e:
        log.warning("rag.recall_llm_failed", error_type=type(e).__name__, partial=bool(st.text))
        degraded.append("recall_llm_failed")
        if not st.text:
            fallback = _facts_reply(facts)
            runtime.stream_writer(DeltaEvent(text=fallback))
            st.text = fallback
    answer = st.text.strip()[:RECALL_MAX_CHARS] or RECALL_NOT_FOUND
    ids = check_identifiers(
        answer,
        facts=facts,
        question=inp.question,
        sources=[t.content for t in history] + [prompting.format_summary(summary)],
    )
    post: dict[str, Any] = {}
    if ids.checked:
        post["identifiers"] = {
            "checked": ids.checked,
            "mismatched": [mask_identifier(k, v) for k, v in ids.mismatched],
        }
    if ids.mismatch:
        degraded.append("identifier_mismatch")
        metrics.inc("aics_identifier_mismatch_total", app_id=str(inp.app_id))
    return merged(
        state,
        answer=answer,
        answer_type="answer",
        decision_reason="intent:recall",
        citations=[],
        usage=st.usage,
        prompt_versions=versions,
        timings={"recall": int((time.perf_counter() - t0) * 1000)},
        degraded=degraded,
        trace={"recall": post} if post else {},
    )
