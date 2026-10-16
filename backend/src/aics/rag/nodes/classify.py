"""condense_classify node (意图识别 §5.2) and route_intent (§6.3).

- `intent.enabled=true`: one JSON-mode call (temperature 0, max_tokens 200) → IntentResult →
  fallback chain (`aics.rag.intent.fallback`). Timeout (`classify_timeout_ms`) / upstream
  error / breaker open → kb_qa + original question. Not retried (§5.2.1).
- `intent.enabled=false`: plain condense — no history → pass-through without an LLM call;
  with history → rewrite (failure → original question); intent fixed kb_qa / fallback.

Parallel classify ∥ retrieve for history-less questions (§5.4) is M2.4.
"""

from __future__ import annotations

import time
from typing import Any

import structlog
from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime

from aics.core.errors import AppError
from aics.rag import prompting
from aics.rag.intent.fallback import ResolvedIntent, fallback, parse_intent_json, resolve
from aics.rag.intent.schema import SLOT_KEYS
from aics.rag.schemas import TokenUsage
from aics.rag.state import RagDeps, RagState, merged

log = structlog.get_logger()

CLASSIFY_MAX_TOKENS = 200  # 意图识别 §5.2.1
CONDENSE_MAX_TOKENS = 200
CONDENSE_TIMEOUT_S = 5.0
OPTIONAL_INTENTS: tuple[str, ...] = ("chitchat", "human", "out_of_scope", "action", "clarify")


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
    if not cfg.intent.enabled:
        resolved, usage, versions = await _condense_only(state, runtime)
    else:
        resolved, usage, versions = await _classify(state, runtime)
    ms = int((time.perf_counter() - t0) * 1000)
    upd = merged(
        state,
        intent=resolved,
        rewritten_query=resolved.rewritten_query,
        prompt_versions=versions,
        timings={"condense_classify": ms},
        trace={"intent": {**resolved.trace(), "mode": "merged"}},
    )
    if usage.calls:
        upd.update(merged(state, usage=usage))
    if resolved.fallback_reason in ("timeout", "upstream_error"):
        upd.update(merged(state, degraded=["classify_failed"]))
    return upd


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
}


def route_intent(state: RagState) -> str:
    return ROUTES[state["intent"].intent]
