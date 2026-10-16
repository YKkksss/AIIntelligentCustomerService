"""guard node: the rule layer (意图识别 §5.1) in front of load_context."""

from __future__ import annotations

import time
from typing import Any

import structlog
from langgraph.runtime import Runtime

from aics.rag.intent.fallback import from_rule
from aics.rag.intent.rules import apply_rules
from aics.rag.state import RagDeps, RagState, merged

log = structlog.get_logger()

ROUTES = {"blocked": "blocked_reply", "human": "handoff", "chitchat": "chitchat_reply"}


async def guard(state: RagState, runtime: Runtime[RagDeps]) -> dict[str, Any]:
    inp = state["input"]
    t0 = time.perf_counter()
    hit = apply_rules(inp.question, inp.config.intent, inp.handoff, inp.refusal_streak)
    us = int((time.perf_counter() - t0) * 1000)
    if hit is None:
        return merged(state, timings={"guard": us})
    resolved = from_rule(inp.question, hit.intent, hit.trace())
    log.info("rag.rule_hit", rule=hit.rule, app_id=str(inp.app_id))
    return merged(
        state,
        intent=resolved,
        rewritten_query=inp.question,
        timings={"guard": us},
        trace={"intent": resolved.trace()},
    )


def route_guard(state: RagState) -> str:
    resolved = state.get("intent")
    if resolved is None or resolved.source != "rule":
        return "load_context"
    return ROUTES[resolved.intent]
