"""The single-turn RAG graph and `run_rag` (RAG 流程设计 §1–§6).

guard → load_context → condense_classify → route_intent
  kb_qa → retrieve → fuse → rerank → judge → generate → postprocess
                                            ↘ refuse | handoff
  chitchat / out_of_scope / clarify → fixed reply;  human / action → handoff

Streaming (§6): nodes push `TraceEvent` (debug only) → `CitationsEvent` (kb_qa answer, before
the first token) → `DeltaEvent`* through the LangGraph custom stream; `run_rag` yields them and
finishes with `RagResult`. Token deltas are pushed by the generate node itself (custom mode)
instead of `stream_mode="messages"`, so classify / judge tokens can never leak into the answer
stream (design discussion 0004 §4).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from langgraph.graph import END, START, StateGraph

from aics.rag import prompting
from aics.rag.llm import LlmGateway
from aics.rag.nodes import answer, classify, context, replies, retrieval
from aics.rag.ports import RagPorts
from aics.rag.schemas import IntentInfo, RagEvent, RagInput, RagResult, TokenUsage
from aics.rag.state import RagDeps, RagState


def build_graph() -> Any:
    g = StateGraph(RagState, context_schema=RagDeps)
    g.add_node("guard", context.guard)
    g.add_node("load_context", context.load_context)
    g.add_node("condense_classify", classify.condense_classify)
    g.add_node("retrieve", retrieval.retrieve)
    g.add_node("fuse", retrieval.fuse)
    g.add_node("rerank", retrieval.rerank)
    g.add_node("judge", answer.judge)
    g.add_node("generate", answer.generate)
    g.add_node("postprocess", answer.postprocess)
    g.add_node("refuse", answer.refuse)
    g.add_node("handoff", replies.handoff)
    g.add_node("chitchat_reply", replies.chitchat_reply)
    g.add_node("out_of_scope_reply", replies.out_of_scope_reply)
    g.add_node("clarify_reply", replies.clarify_reply)

    g.add_edge(START, "guard")
    g.add_edge("guard", "load_context")
    g.add_edge("load_context", "condense_classify")
    g.add_conditional_edges(
        "condense_classify", classify.route_intent, sorted(set(classify.ROUTES.values()))
    )
    g.add_edge("retrieve", "fuse")
    g.add_edge("fuse", "rerank")
    g.add_edge("rerank", "judge")
    g.add_conditional_edges("judge", answer.route_judge, ["generate", "refuse", "handoff"])
    g.add_edge("generate", "postprocess")
    for terminal in (
        "postprocess",
        "refuse",
        "handoff",
        "chitchat_reply",
        "out_of_scope_reply",
        "clarify_reply",
    ):
        g.add_edge(terminal, END)
    return g.compile(name="aics_rag")


GRAPH = build_graph()


def to_result(state: RagState, llm: LlmGateway) -> RagResult:
    inp = state["input"]
    resolved = state.get("intent")
    versions = dict(state.get("prompt_versions", {}))
    trace = answer.build_trace(state)
    return RagResult(
        answer=state.get("answer", ""),
        answer_type=state.get("answer_type", "answer"),  # type: ignore[arg-type]
        handoff_reason=state.get("handoff_reason"),
        decision_reason=state.get("decision_reason", "answered"),
        intent=IntentInfo(
            name=resolved.intent,  # type: ignore[arg-type]
            confidence=resolved.confidence,
            source=resolved.source,  # type: ignore[arg-type]
        )
        if resolved
        else IntentInfo(),
        slots=dict(resolved.slots) if resolved else {},
        rewritten_query=state.get("rewritten_query"),
        citations=list(state.get("citations", [])),
        prompt_versions=versions,
        prompt_version=prompting.prompt_version_string(versions),
        llm_model=llm.model_name(inp.config.llm_model),
        usage=state.get("usage", TokenUsage()),
        timings=dict(state.get("timings", {})),
        degraded=list(state.get("degraded", [])),
        context_usage=dict(state.get("context_usage", {})),
        trace=trace,
    )


async def run_rag(inp: RagInput, ports: RagPorts) -> AsyncIterator[RagEvent]:
    """Run one turn. Yields stream events; the last event is always `RagResult`.

    Raises (chat.service maps them to `answer_type=error`): `LlmCallError` /
    `LlmUnavailableError` from generate, `RetrievalUnavailableError` when retrieval is down.
    """
    if ports.llm is None:
        raise ValueError("RagPorts.llm is required")
    llm = LlmGateway(ports.llm)
    deps = RagDeps(ports=ports, llm=llm)
    final: dict[str, Any] | None = None
    init: RagState = {"input": inp, "degraded": [], "timings": {}, "usage": TokenUsage()}
    async for mode, chunk in GRAPH.astream(init, context=deps, stream_mode=["custom", "values"]):
        if mode == "custom":
            yield chunk
        else:
            final = chunk
    if final is None:  # pragma: no cover - LangGraph always emits the final values
        raise RuntimeError("rag graph produced no state")
    yield to_result(final, llm)  # type: ignore[arg-type]


async def run_rag_collect(inp: RagInput, ports: RagPorts) -> tuple[RagResult, list[RagEvent]]:
    """Non-streaming helper (blocking API / eval): returns the result and all events."""
    events: list[RagEvent] = []
    async for ev in run_rag(inp, ports):
        events.append(ev)
    result = events[-1]
    if not isinstance(result, RagResult):  # pragma: no cover
        raise RuntimeError("run_rag must end with RagResult")
    return result, events
