"""SSE encoding of turn events (开放接口参考 §1 流式, RAG 流程设计 §6).

Order: start → trace? (debug only) → citations? (kb_qa answer only) → delta* → end,
or `error` then close. `: ping` comment every HEARTBEAT_S while nothing else is sent.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from aics.modules.chat.service import (
    ErrorEvent,
    StartEvent,
    TurnEvent,
    TurnHandle,
    TurnResult,
    citation_dict,
)
from aics.rag.schemas import CitationsEvent, DeltaEvent, TraceEvent

HEARTBEAT_S = 15.0  # 接口规范 §8


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)


def frame(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {_json(data)}\n\n"


def usage_of(r: TurnResult) -> dict[str, int]:
    return {
        "prompt_tokens": r.prompt_tokens,
        "completion_tokens": r.completion_tokens,
        "total_tokens": r.prompt_tokens + r.completion_tokens,
    }


def end_payload(r: TurnResult) -> dict[str, Any]:
    return {
        "answer_type": r.answer_type,
        "handoff": r.handoff,
        "handoff_reason": r.handoff_reason,
        "intent": r.intent,
        "used_citations": r.used_citations,
        "usage": usage_of(r),
        "latency_ms": r.latency_ms,
        "reply_by": r.reply_by,
        "handoff_status": r.handoff_status,
        "previous_conversation_id": r.previous_conversation_id,
        # Final answer after citation clean-up (deltas cannot be retracted, RAG 流程设计 §6).
        "answer": r.answer,
    }


def blocking_body(r: TurnResult) -> dict[str, Any]:
    """Blocking response (citations = only the entries actually cited)."""
    return {
        "conversation_id": r.conversation_id,
        "previous_conversation_id": r.previous_conversation_id,
        "message_id": r.message_id,
        "reply_by": r.reply_by,
        "handoff_status": r.handoff_status,
        "answer": r.answer,
        "answer_type": r.answer_type,
        "handoff": r.handoff,
        "handoff_reason": r.handoff_reason,
        "intent": r.intent,
        "citations": r.citations,
        "usage": usage_of(r),
        "latency_ms": r.latency_ms,
    }


def encode(ev: TurnEvent, request_id: str | None) -> str:
    if isinstance(ev, StartEvent):
        return frame("start", {"conversation_id": ev.conversation_id, "message_id": ev.message_id})
    if isinstance(ev, CitationsEvent):
        return frame("citations", {"citations": [citation_dict(c) for c in ev.citations]})
    if isinstance(ev, TraceEvent):
        return frame("trace", ev.trace)
    if isinstance(ev, DeltaEvent):
        return frame("delta", {"text": ev.text})
    if isinstance(ev, TurnResult):
        return frame("end", end_payload(ev))
    if isinstance(ev, ErrorEvent):
        return frame("error", {"code": ev.code, "message": ev.message, "request_id": request_id})
    raise TypeError(f"unknown turn event {type(ev).__name__}")  # pragma: no cover


async def stream(
    handle: TurnHandle, request_id: str | None, *, heartbeat_s: float = HEARTBEAT_S
) -> AsyncIterator[str]:
    """Response body generator. Leaving early (client disconnect → the server cancels this
    generator) cancels the turn, which then persists the partial answer as interrupted."""
    try:
        while True:
            try:
                ev = await handle.next_event(heartbeat_s)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            if ev is None:
                return
            yield encode(ev, request_id)
    finally:
        handle.cancel()  # no-op once the answer is complete
