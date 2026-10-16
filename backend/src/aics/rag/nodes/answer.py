"""judge → generate → postprocess, and refuse (RAG 流程设计 §4, §6; 安全设计 §3.4).

judge (`decision_reason`):
- no candidates → refuse `no_candidates` (no LLM call);
- rerank scores present: top < `rerank_threshold` → `below_threshold`; top ≥ `judge_gray_upper`
  → `above_gray_upper`; gray zone → LLM (`judge_llm_yes` / `judge_llm_no`) when
  `judge_with_llm`, else `gray_answerable`;
- no rerank scores (rerank off / failed / noop) → always LLM;
- LLM timeout / error / unparsable → unanswerable `judge_failed` (+ degraded `judge_failed`).
Unanswerable goes to handoff when `handoff.enabled and handoff.on_unanswerable`, else refuse.
"""

from __future__ import annotations

import json
import time
from typing import Any

import structlog
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.runtime import Runtime

from aics.core import metrics
from aics.core.errors import AppError
from aics.core.masking import mask_identifier
from aics.rag import prompting
from aics.rag.citations import leaks_instructions, parse_citations
from aics.rag.identifiers import check_identifiers
from aics.rag.llm import StreamState
from aics.rag.retrieval import Candidate
from aics.rag.schemas import (
    Citation,
    CitationsEvent,
    DeltaEvent,
    RunMode,
    TraceEvent,
)
from aics.rag.state import RagDeps, RagState, merged

log = structlog.get_logger()

JUDGE_MAX_TOKENS = 200
JUDGE_TIMEOUT_S = 8.0
JUDGE_CONTEXT_CHARS = 800  # per source; judge only needs the gist
SNIPPET_CHARS = 200


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


# ---------------------------------------------------------------- judge
def _parse_answerable(text: str) -> bool | None:
    raw = text.strip().strip("`").removeprefix("json").strip()
    try:
        data = json.loads(raw)
    except ValueError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(raw[start : end + 1])
        except ValueError:
            return None
    v = data.get("answerable") if isinstance(data, dict) else None
    return v if isinstance(v, bool) else None


async def judge(state: RagState, runtime: Runtime[RagDeps]) -> dict[str, Any]:
    inp = state["input"]
    cfg = inp.config
    cands: list[Candidate] = state.get("reranked", [])
    t0 = time.perf_counter()
    info: dict[str, Any] = {"rerank_applied": bool(state.get("rerank_applied"))}
    if not cands:
        return _decide(state, inp, False, "no_candidates", info, t0)
    top = cands[0].rerank_score if state.get("rerank_applied") else None
    info["top_score"] = top
    if top is not None:
        if top < cfg.rerank_threshold:
            return _decide(state, inp, False, "below_threshold", info, t0)
        if top >= cfg.judge_gray_upper:
            return _decide(state, inp, True, "above_gray_upper", info, t0)
        if not cfg.judge_with_llm:
            return _decide(state, inp, True, "gray_answerable", info, t0)
    prompt = prompting.load("judge")
    text = prompting.render(
        prompt,
        context=prompting.format_context(cands, max_chars=JUDGE_CONTEXT_CHARS),
        question=" ".join((state.get("rewritten_query") or inp.question).split()),
    )
    extra: dict[str, Any] = {"prompt_versions": {"judge": str(prompt.version)}}
    try:
        reply = await runtime.context.llm.complete(
            [HumanMessage(text)],
            node="judge",
            model=cfg.llm_model,
            temperature=0.0,
            max_tokens=JUDGE_MAX_TOKENS,
            timeout_s=JUDGE_TIMEOUT_S,
            json_mode=True,
        )
    except (TimeoutError, AppError) as e:
        info["llm_error"] = getattr(e, "code", type(e).__name__)
        return _decide(
            state, inp, False, "judge_failed", info, t0, degraded=["judge_failed"], **extra
        )
    extra["usage"] = reply.usage
    verdict = _parse_answerable(reply.text)
    info["llm_verdict"] = verdict
    if verdict is None:
        return _decide(
            state, inp, False, "judge_failed", info, t0, degraded=["judge_failed"], **extra
        )
    return _decide(
        state, inp, verdict, "judge_llm_yes" if verdict else "judge_llm_no", info, t0, **extra
    )


def _decide(
    state: RagState,
    inp: Any,
    answerable: bool,
    reason: str,
    info: dict[str, Any],
    t0: float,
    **extra: Any,
) -> dict[str, Any]:
    if answerable:
        decision = "answer"
    elif inp.handoff.enabled and inp.handoff.on_unanswerable:
        decision = "handoff"
    else:
        decision = "refuse"
    return merged(
        state,
        decision=decision,
        decision_reason=reason,
        judge={**info, "answerable": answerable, "reason": reason},
        timings={"judge": _ms(t0)},
        trace={"judge": {**info, "answerable": answerable, "reason": reason}},
        **extra,
    )


def route_judge(state: RagState) -> str:
    return {"answer": "generate", "refuse": "refuse", "handoff": "handoff"}[state["decision"]]


# ---------------------------------------------------------------- generate
def citation_of(i: int, c: Candidate) -> Citation:
    return Citation(
        index=i,
        chunk_id=c.chunk.chunk_id,
        parent_id=c.parent.parent_id if c.parent else None,
        document_id=c.chunk.document_id,
        document_title=c.chunk.document_title,
        heading_path=c.heading_path,
        page=c.parent.page_from if c.parent else c.chunk.page_from,
        snippet=c.text[:SNIPPET_CHARS],
        rerank_score=c.rerank_score,
    )


def _background(state: RagState) -> str:
    parts = [
        prompting.format_facts(state.get("facts") or {}),
        prompting.format_summary(state.get("summary")),
    ]
    return "\n".join(p for p in parts if p)


def generate_messages(state: RagState) -> tuple[list[BaseMessage], int]:
    inp = state["input"]
    p = inp.profile
    prompt = prompting.load("generate")
    system = prompting.render(
        prompt,
        app_name=p.name,
        system_prompt=(p.system_prompt or "").strip(),
        fallback_text=p.fallback_text,
        answer_style=prompting.ANSWER_STYLES.get(p.answer_style or "concise", p.answer_style or ""),
        language=prompting.LANGUAGES.get(p.language, p.language),
        facts_and_summary=_background(state),
        context=prompting.format_context(state.get("reranked", [])),
    )
    msgs: list[BaseMessage] = [SystemMessage(system)]
    for t in state.get("history") or []:
        msgs.append(HumanMessage(t.content) if t.role == "user" else AIMessage(t.content))
    msgs.append(HumanMessage(inp.question))
    return msgs, prompt.version


def emit_trace(state: RagState, runtime: Runtime[RagDeps], **extra: Any) -> None:
    if state["input"].run_mode == RunMode.debug:
        runtime.stream_writer(TraceEvent(trace=build_trace(state, **extra)))


def build_trace(state: RagState, **extra: Any) -> dict[str, Any]:
    tr: dict[str, Any] = dict(state.get("trace", {}))
    if "fused" in state:
        tr["retrieval"] = {
            "query": state.get("rewritten_query"),
            "tags": state.get("tags", []),
            "vector": [(str(h.chunk_id), round(h.score, 4)) for h in state.get("vector_hits", [])],
            "keyword": [
                (str(h.chunk_id), round(h.score, 4)) for h in state.get("keyword_hits", [])
            ],
            "fusion": state["input"].config.fusion,
            "fused": [c.trace() for c in state.get("fused", [])],
            "reranked": [c.trace() for c in state.get("reranked", [])],
            "rerank_applied": bool(state.get("rerank_applied")),
        }
    tr["degraded"] = list(state.get("degraded", []))
    tr["timings"] = dict(state.get("timings", {}))
    tr.update(extra)
    return tr


async def generate(state: RagState, runtime: Runtime[RagDeps]) -> dict[str, Any]:
    inp = state["input"]
    cfg = inp.config
    cands: list[Candidate] = state.get("reranked", [])
    emit_trace(state, runtime)
    runtime.stream_writer(
        CitationsEvent(citations=[citation_of(i, c) for i, c in enumerate(cands, start=1)])
    )
    msgs, version = generate_messages(state)
    st = StreamState()
    t0 = time.perf_counter()
    async for piece in runtime.context.llm.stream(
        msgs,
        st,
        node="generate",
        model=cfg.llm_model,
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
        thinking=cfg.llm_thinking,
    ):
        runtime.stream_writer(DeltaEvent(text=piece))
    timings = {"generate": _ms(t0)}
    if st.first_token_ms is not None:
        timings["first_token"] = st.first_token_ms
    return merged(
        state,
        answer=st.text,
        answer_type="answer",
        usage=st.usage,
        prompt_versions={"generate": str(version)},
        timings=timings,
    )


# ---------------------------------------------------------------- postprocess
def _protected_rules() -> str:
    """Fixed rule text of the generate prompt (app system_prompt excluded: answers may
    legitimately repeat business facts written there)."""
    body = prompting.load("generate").body
    start = body.find("规则（")
    end = body.find("<conversation_background>")
    return body[start:end] if 0 <= start < end else ""


async def postprocess(state: RagState, runtime: Runtime[RagDeps]) -> dict[str, Any]:
    inp = state["input"]
    cands: list[Candidate] = state.get("reranked", [])
    answer = state.get("answer", "")
    degraded: list[str] = []
    if not answer.strip():
        log.warning("rag.empty_answer", app_id=str(inp.app_id))
        return merged(
            state,
            answer=inp.profile.fallback_text,
            answer_type="refused",
            decision_reason="empty_answer",
            citations=[],
            degraded=["empty_answer"],
        )
    if leaks_instructions(answer, [_protected_rules()]):
        log.warning("rag.output_filtered", app_id=str(inp.app_id))
        return merged(
            state,
            answer=inp.profile.fallback_text,
            answer_type="refused",
            decision_reason="output_filtered",
            citations=[],
            degraded=["output_filtered"],
            trace={"postprocess": {"output_filtered": True}},
        )
    parsed = parse_citations(answer, len(cands))
    citations = [citation_of(n, cands[n - 1]) for n in parsed.used]
    if not citations:
        degraded.append("no_citation")
    post: dict[str, Any] = {"cited": parsed.used, "dropped": parsed.dropped}
    ids = check_identifiers(
        parsed.text,
        facts=state.get("facts") or {},
        question=inp.question,
        sources=[c.text for c in cands],
    )
    if ids.checked:
        post["identifiers"] = {
            "checked": ids.checked,
            "mismatched": [mask_identifier(k, v) for k, v in ids.mismatched],
        }
    if ids.mismatch:
        degraded.append("identifier_mismatch")
        metrics.inc("aics_identifier_mismatch_total", app_id=str(inp.app_id))
        log.warning(
            "rag.identifier_mismatch",
            app_id=str(inp.app_id),
            values=[mask_identifier(k, v) for k, v in ids.mismatched],
        )
    return merged(
        state,
        answer=parsed.text,
        citations=citations,
        degraded=degraded,
        trace={"postprocess": post},
    )


# ---------------------------------------------------------------- refuse
async def refuse(state: RagState, runtime: Runtime[RagDeps]) -> dict[str, Any]:
    text = state["input"].profile.fallback_text
    emit_trace(state, runtime)
    runtime.stream_writer(DeltaEvent(text=text))
    return merged(state, answer=text, answer_type="refused", citations=[])
