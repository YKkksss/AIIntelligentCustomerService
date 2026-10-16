"""guard and load_context nodes (RAG 流程设计 §4; 对话上下文管理 §6)."""

from __future__ import annotations

import time
from typing import Any

import structlog
from langgraph.runtime import Runtime

from aics.rag.ports import ContextRequest
from aics.rag.state import RagDeps, RagState, merged

log = structlog.get_logger()


async def guard(state: RagState, runtime: Runtime[RagDeps]) -> dict[str, Any]:
    """Rule layer hook. M2.3 lets everything through; the rules (sensitive words, handoff
    keywords, refusal streak, chitchat templates) are M2.4 (`aics.rag.intent`)."""
    return merged(state, timings={"guard": 0})


async def load_context(state: RagState, runtime: Runtime[RagDeps]) -> dict[str, Any]:
    inp = state["input"]
    cfg = inp.config
    t0 = time.perf_counter()
    memory = runtime.context.ports.memory
    degraded: list[str] = []
    if memory is not None:
        try:
            bundle = await memory.load_context(
                ContextRequest(
                    app_id=inp.app_id,
                    conversation_id=inp.conversation_id,
                    inline_history=inp.inline_history,
                    question=inp.question,
                    budget=cfg.history_token_budget,
                    keep_recent_turns=cfg.keep_recent_turns,
                    enable_facts=cfg.enable_facts,
                    enable_history_recall=cfg.enable_history_recall,
                    temp_summary=cfg.openai_temp_summary,
                )
            )
        except Exception as e:  # read failure → recent inline turns only
            log.warning("rag.context_failed", error_type=type(e).__name__)
            bundle = None
            degraded.append("context_degraded")
        if bundle is not None:
            return merged(
                state,
                history=list(bundle.history),
                facts=dict(bundle.facts) if cfg.enable_facts else {},
                summary=bundle.summary,
                recalled=list(bundle.recalled),
                context_usage=dict(bundle.usage),
                degraded=list(bundle.flags),
                timings={"load_context": int((time.perf_counter() - t0) * 1000)},
            )
    # No memory port (stateless / M2.3) or failure: last keep_recent_turns turns verbatim.
    turns = list(inp.inline_history or [])
    keep = cfg.keep_recent_turns * 2  # a turn = user + assistant message
    history = turns[-keep:] if keep else []
    return merged(
        state,
        history=history,
        facts={},
        summary=None,
        recalled=[],
        context_usage={},
        degraded=degraded,
        timings={"load_context": int((time.perf_counter() - t0) * 1000)},
    )
