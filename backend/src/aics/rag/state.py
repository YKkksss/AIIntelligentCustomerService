"""Graph state and run-time dependencies (RAG 流程设计 §3)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypedDict

from aics.rag.intent.fallback import ResolvedIntent
from aics.rag.llm import LlmGateway
from aics.rag.ports import ChatTurn, RagPorts, RecalledSnippet
from aics.rag.retrieval import Candidate, RouteHit
from aics.rag.schemas import Citation, RagInput, TokenUsage


class RagState(TypedDict, total=False):
    input: RagInput
    history: list[ChatTurn]
    facts: dict[str, str]
    summary: dict[str, Any] | None
    recalled: list[RecalledSnippet]
    context_usage: dict[str, int]
    rewritten_query: str
    intent: ResolvedIntent
    vector_hits: list[RouteHit]
    keyword_hits: list[RouteHit]
    fused: list[Candidate]
    reranked: list[Candidate]
    rerank_applied: bool
    tags: list[str]
    decision: str  # answer / refuse / handoff
    decision_reason: str
    judge: dict[str, Any]
    answer: str
    answer_type: str
    handoff_reason: str | None
    citations: list[Citation]
    prompt_versions: dict[str, str]
    usage: TokenUsage
    timings: dict[str, int]
    degraded: list[str]
    trace: dict[str, Any]


@dataclass(frozen=True)
class RagDeps:
    """LangGraph run-time context (not part of the state)."""

    ports: RagPorts
    llm: LlmGateway


def merged(state: RagState, **updates: Any) -> dict[str, Any]:
    """Helper for the accumulating keys: `degraded` (list), `timings` / `prompt_versions` /
    `trace` (dict merge) and `usage` (sum)."""
    out: dict[str, Any] = {}
    for k, v in updates.items():
        if k == "degraded":
            cur = list(state.get("degraded", []))
            out[k] = cur + [x for x in v if x not in cur]
        elif k in ("timings", "prompt_versions", "trace"):
            out[k] = {**state.get(k, {}), **v}
        elif k == "usage":
            out[k] = state.get("usage", TokenUsage()).add(v)
        else:
            out[k] = v
    return out
