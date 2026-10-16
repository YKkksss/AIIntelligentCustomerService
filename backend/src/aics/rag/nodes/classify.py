"""condense_classify node (意图识别 §5.2) and route_intent (§6.3).

- `intent.enabled=true`: one JSON-mode call (temperature 0, max_tokens 200) → IntentResult →
  fallback chain (`aics.rag.intent.fallback`). Timeout (`classify_timeout_ms`) / upstream
  error / breaker open → kb_qa + original question. Not retried (§5.2.1).
- `intent.enabled=false`: plain condense — no history → pass-through without an LLM call;
  with history → rewrite (failure → original question); intent fixed kb_qa / fallback.

- no history (and intent enabled): classify ∥ retrieve + fuse on the original question
  (§5.4); kb_qa keeps the result (rewritten_query = original question), other intents
  cancel it — except out_of_scope when the safeguard is on (`scope_check`).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog
from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime

from aics.core.errors import AppError
from aics.rag import prompting
from aics.rag import retrieval as r
from aics.rag.intent.fallback import ResolvedIntent, fallback, parse_intent_json, resolve
from aics.rag.intent.schema import SLOT_KEYS
from aics.rag.nodes.retrieval import retrieve_and_fuse
from aics.rag.schemas import TokenUsage
from aics.rag.state import RagDeps, RagState, merged

log = structlog.get_logger()

CLASSIFY_MAX_TOKENS = 200  # 意图识别 §5.2.1
CONDENSE_MAX_TOKENS = 200
CONDENSE_TIMEOUT_S = 5.0
OPTIONAL_INTENTS: tuple[str, ...] = (
    "chitchat",
    "human",
    "out_of_scope",
    "action",
    "clarify",
    "recall",
)


def _data_vars(state: RagState) -> dict[str, str]:
    return {
        "facts": prompting.format_facts(state.get("facts") or {}),
        "summary": prompting.format_summary(state.get("summary")),
        "recall": prompting.format_recall(state.get("recalled") or []),
        "history": prompting.format_history(state.get("history") or []),
        "question": " ".join(state["input"].question.split()),
    }


def _disabled_note(enabled: list[str]) -> str:
    off = [i for i in OPTIONAL_INTENTS if i not in enabled]
    return f"以下意图本应用未启用：{'、'.join(off)}，不要选择。" if off else ""


async def condense_classify(state: RagState, runtime: Runtime[RagDeps]) -> dict[str, Any]:
    inp = state["input"]
    cfg = inp.config
    t0 = time.perf_counter()
    parallel = cfg.intent.enabled and not state.get("history")
    task: asyncio.Task[dict[str, Any]] | None = None
    if parallel:
        # 意图识别 §5.4: no history → classify ∥ retrieve(original question) + fuse.
        task = asyncio.create_task(retrieve_and_fuse(inp, runtime.context.ports, inp.question))
    try:
        if not cfg.intent.enabled:
            resolved, usage, versions = await _condense_only(state, runtime)
        else:
            resolved, usage, versions = await _classify(state, runtime)
    except BaseException:
        if task is not None:
            task.cancel()
        raise
    ms = int((time.perf_counter() - t0) * 1000)
    extra: dict[str, Any] = {}
    degraded: list[str] = []
    timings = {"condense_classify": ms}
    if task is not None:
        resolved.mode = "parallel"
        keep = resolved.intent == "kb_qa" or (
            resolved.intent == "out_of_scope" and scope_check_enabled(state)
        )
        if keep:
            resolved.rewritten_query = inp.question  # retrieval already used the original
            try:
                ret = await task
            except Exception as e:
                if resolved.intent == "kb_qa":
                    raise
                log.warning("rag.scope_check_retrieval_failed", error_type=type(e).__name__)
                ret = None
            if ret is not None:
                degraded += ret.pop("degraded")
                timings.update(ret.pop("timings"))
                extra.update(ret)
        else:
            task.cancel()
            log.info("rag.retrieval_cancelled", intent=resolved.intent)
            extra["trace"] = {"retrieval_cancelled": True}
    if resolved.fallback_reason in ("timeout", "upstream_error"):
        degraded.append("classify_failed")
    trace = {**extra.pop("trace", {}), "intent": resolved.trace()}
    upd = merged(
        state,
        intent=resolved,
        rewritten_query=resolved.rewritten_query,
        prompt_versions=versions,
        timings=timings,
        trace=trace,
        degraded=degraded,
        **({"usage": usage} if usage.calls else {}),
    )
    upd.update(extra)
    return upd


def scope_check_enabled(state: RagState) -> bool:
    cfg = state["input"].config
    return cfg.intent.out_of_scope_check and cfg.enable_rerank


async def _classify(
    state: RagState, runtime: Runtime[RagDeps]
) -> tuple[ResolvedIntent, TokenUsage, dict[str, str]]:
    inp = state["input"]
    cfg = inp.config
    prompt = prompting.load("condense_classify")
    versions = {"condense_classify": str(prompt.version)}
    text = prompting.render(
        prompt,
        app_name=inp.profile.name,
        app_scope=prompting.app_scope(inp.profile.description, inp.profile.system_prompt),
        disabled_intents_note=_disabled_note(cfg.intent.enabled_intents),
        slot_keys=", ".join(SLOT_KEYS),
        **_data_vars(state),
    )
    try:
        reply = await runtime.context.llm.complete(
            [HumanMessage(text)],
            node="condense_classify",
            model=cfg.llm_model,
            temperature=0.0,
            max_tokens=CLASSIFY_MAX_TOKENS,
            timeout_s=cfg.intent.classify_timeout_ms / 1000,
            json_mode=True,
        )
    except TimeoutError:
        return fallback(inp.question, "timeout"), TokenUsage(calls=1), versions
    except AppError as e:  # LlmCallError / LlmUnavailableError
        log.warning("rag.classify_failed", code=e.code)
        return fallback(inp.question, "upstream_error"), TokenUsage(calls=1), versions
    result, salvaged = parse_intent_json(reply.text)
    if result is None:
        log.info("rag.classify_parse_error", chars=len(reply.text))
        return fallback(inp.question, "parse_error", rewritten=salvaged), reply.usage, versions
    resolved = resolve(
        result,
        question=inp.question,
        cfg=cfg.intent,
        handoff=inp.handoff,
        last_assistant_was_clarify=inp.last_assistant_was_clarify,
    )
    return resolved, reply.usage, versions


async def _condense_only(
    state: RagState, runtime: Runtime[RagDeps]
) -> tuple[ResolvedIntent, TokenUsage, dict[str, str]]:
    inp = state["input"]
    out = fallback(inp.question, "intent_disabled")
    out.confidence = 1.0
    if not state.get("history"):
        return out, TokenUsage(), {}
    prompt = prompting.load("condense")
    versions = {"condense": str(prompt.version)}
    try:
        reply = await runtime.context.llm.complete(
            [HumanMessage(prompting.render(prompt, **_data_vars(state)))],
            node="condense",
            model=inp.config.llm_model,
            temperature=0.0,
            max_tokens=CONDENSE_MAX_TOKENS,
            timeout_s=CONDENSE_TIMEOUT_S,
        )
    except (TimeoutError, AppError):
        return out, TokenUsage(calls=1), versions
    rewritten = " ".join(reply.text.split())[:500]
    if rewritten:
        out.rewritten_query = rewritten
    return out, reply.usage, versions


ROUTES = {
    "kb_qa": "retrieve",
    "chitchat": "chitchat_reply",
    "human": "handoff",
    "action": "handoff",
    "out_of_scope": "out_of_scope_reply",
    "clarify": "clarify_reply",
    "recall": "recall_reply",
}


def route_intent(state: RagState) -> str:
    intent = state["intent"].intent
    if intent == "kb_qa" and "fused" in state:
        return "rerank"  # parallel mode already retrieved
    if intent == "out_of_scope" and scope_check_enabled(state):
        return "scope_check"
    return ROUTES[intent]


async def scope_check(state: RagState, runtime: Runtime[RagDeps]) -> dict[str, Any]:
    """Out-of-scope safeguard (设计讨论 0005): verify an LLM `out_of_scope` verdict against
    the knowledge base. Retrieve (unless parallel mode already did) + rerank; a top rerank
    score ≥ `intent.out_of_scope_override_score` overrides the intent to kb_qa
    (source=fallback, fallback_reason=kb_evidence) and continues at judge. Any failure keeps
    the out_of_scope reply (the safeguard never makes things worse)."""
    inp = state["input"]
    cfg = inp.config
    resolved = state["intent"]
    t0 = time.perf_counter()
    ports = runtime.context.ports
    upd: dict[str, Any] = {}
    info: dict[str, Any] = {"threshold": cfg.intent.out_of_scope_override_score}
    try:
        if "fused" in state:
            fused = state["fused"]
            query = inp.question
        else:
            query = resolved.rewritten_query or inp.question
            ret = await retrieve_and_fuse(inp, ports, query)
            ret.pop("degraded")
            ret.pop("timings")
            upd.update(ret)
            fused = ret["fused"]
        top, applied, _ = await r.rerank(query, fused, cfg, reranker=ports.reranker)
    except Exception as e:
        log.warning("rag.scope_check_failed", error_type=type(e).__name__)
        info["error"] = type(e).__name__
        resolved.scope_check = info
        return merged(state, timings={"scope_check": _ms(t0)}, trace={"intent": resolved.trace()})
    score = top[0].rerank_score if applied and top else None
    info["top_score"] = score
    override = score is not None and score >= cfg.intent.out_of_scope_override_score
    info["override"] = override
    resolved.scope_check = info
    if override:
        resolved.intent, resolved.source = "kb_qa", "fallback"
        resolved.fallback_reason = "kb_evidence"
        resolved.rewritten_query = query
        upd.update(reranked=top, rerank_applied=applied)
        log.info("rag.out_of_scope_overridden", score=round(score or 0, 4))
    return merged(
        state,
        intent=resolved,
        rewritten_query=resolved.rewritten_query,
        timings={"scope_check": _ms(t0)},
        trace={"intent": resolved.trace()},
        **upd,
    )


def route_scope(state: RagState) -> str:
    return "judge" if state["intent"].intent == "kb_qa" else "out_of_scope_reply"


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)
