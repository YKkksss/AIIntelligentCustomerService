"""M2.6 ③ SSE framing, heartbeat and cancel-on-exit (开放接口参考 §1 流式)."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from aics.modules.chat import sse
from aics.modules.chat.service import ErrorEvent, StartEvent, TurnResult
from aics.rag.schemas import DeltaEvent


class FakeHandle:
    def __init__(self, events: list[Any], gap_s: float = 0.0) -> None:
        self.events = list(events)
        self.gap_s = gap_s
        self.cancelled = 0

    async def next_event(self, timeout_s: float | None = None) -> Any:
        if self.gap_s:  # the first wait exceeds the heartbeat interval once
            gap, self.gap_s = self.gap_s, 0.0
            await asyncio.sleep(min(gap, timeout_s or gap))
            raise TimeoutError
        return self.events.pop(0) if self.events else None

    def cancel(self) -> None:
        self.cancelled += 1


def result(**kw: Any) -> TurnResult:
    base: dict[str, Any] = {
        "conversation_id": uuid.uuid4(),
        "previous_conversation_id": None,
        "message_id": uuid.uuid4(),
        "user_message_id": uuid.uuid4(),
        "reply_by": "ai",
        "handoff_status": "none",
        "answer": "ok",
        "answer_type": "answer",
    }
    base.update(kw)
    return TurnResult(**base)


def test_frames() -> None:
    cid, mid = uuid.uuid4(), uuid.uuid4()
    f = sse.encode(StartEvent(cid, mid, None), "rid")
    assert f.startswith("event: start\ndata: ") and f.endswith("\n\n")
    assert json.loads(f.split("data: ", 1)[1]) == {
        "conversation_id": str(cid),
        "message_id": str(mid),
    }
    assert json.loads(sse.encode(DeltaEvent(text="退货"), None).split("data: ", 1)[1]) == {
        "text": "退货"
    }
    err = sse.encode(ErrorEvent("UPSTREAM_TIMEOUT", "超时", 504), "r1")
    assert err.startswith("event: error") and '"request_id":"r1"' in err
    end = json.loads(sse.encode(result(), None).split("data: ", 1)[1])
    assert set(end) >= {
        "answer_type", "handoff", "handoff_reason", "intent", "used_citations", "usage",
        "latency_ms", "reply_by", "handoff_status", "previous_conversation_id",
    }  # fmt: skip
    assert end["usage"] == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


async def test_heartbeat_and_cancel_on_exit() -> None:
    h = FakeHandle([DeltaEvent(text="a")], gap_s=0.05)
    out = [x async for x in sse.stream(h, None, heartbeat_s=0.01)]  # type: ignore[arg-type]
    assert out[0] == ": ping\n\n" and any(x.startswith("event: delta") for x in out)
    assert h.cancelled == 1  # always called; the real handle ignores it after completion


async def test_consumer_leaving_early_cancels_turn() -> None:
    h = FakeHandle([DeltaEvent(text="a"), DeltaEvent(text="b")])
    gen = sse.stream(h, None)  # type: ignore[arg-type]
    assert (await gen.__anext__()).startswith("event: delta")
    await gen.aclose()  # what the server does on client disconnect
    assert h.cancelled == 1


def test_cursor_roundtrip_and_garbage() -> None:
    import pytest

    from aics.core.errors import AppError
    from aics.modules.chat.router import _decode_cursor, _encode_cursor

    mid = uuid.uuid4()
    assert _decode_cursor(_encode_cursor(mid)) == mid
    for bad in ("zzz", _encode_cursor(mid)[:-3], "eDo" + "x"):
        with pytest.raises(AppError):
            _decode_cursor(bad)


def test_blocking_body_fields() -> None:
    body = sse.blocking_body(result(reply_by="none", answer=None, answer_type=None))
    assert list(body) == [
        "conversation_id", "previous_conversation_id", "message_id", "reply_by",
        "handoff_status", "answer", "answer_type", "handoff", "handoff_reason", "intent",
        "citations", "usage", "latency_ms",
    ]  # fmt: skip
