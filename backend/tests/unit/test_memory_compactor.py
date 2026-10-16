"""M2.5 ③: rolling summary + fact extraction (对话上下文管理 §7)."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import openai
import pytest

from aics.core import metrics
from aics.memory.compactor import (
    CompactionUpstreamError,
    Compactor,
    CompactParams,
    next_batch,
    overflow_turns,
    uncovered_tokens,
)
from aics.memory.context import ContextQuery
from aics.memory.locks import LocalLock
from aics.memory.service import MemoryService, in_memory_units, run_compaction_job
from aics.memory.store import InMemoryConversationStore
from tests.memory_support import FakeMemoryLLM, default_responder

APP = uuid.uuid4()
P = CompactParams(keep_recent_turns=4, compact_trigger_tokens=600)


def conversation(n: int, size: int = 30) -> tuple[InMemoryConversationStore, Any]:
    store = InMemoryConversationStore()
    cid = store.create_conversation(APP)
    for i in range(n):
        text = (
            f"第{i}轮" + ("订单号 A2026101400" + str(i % 10) if i in (2, 7) else "") + "说明" * size
        )
        store.add_message(APP, cid, "user", text)
        store.add_message(APP, cid, "assistant", "好的" * size, answer_type="answer")
    return store, cid


def service(
    store: InMemoryConversationStore, llm: FakeMemoryLLM | None = None, **kw: Any
) -> tuple[MemoryService, FakeMemoryLLM, Compactor]:
    llm = llm or FakeMemoryLLM()
    comp = Compactor(in_memory_units(store), llm, kw.pop("lock", LocalLock()))
    return MemoryService(in_memory_units(store), compactor=comp, **kw), llm, comp


async def test_thirty_turns_trigger_compaction_and_version_increments() -> None:
    metrics.reset()
    store, cid = conversation(30)
    svc, llm, _ = service(store)
    trig, res = await svc.maybe_compact(APP, cid, P, run_mode="eval")
    assert trig == "ran" and res is not None and res.result == "success"
    st = await store.get_summary_state(APP, cid)
    assert st.version == res.version == res.batches >= 1
    turns = await store.turns_after(APP, cid, st.until_msg_id)
    assert len(turns) == P.keep_recent_turns  # everything but the floor is summarised
    assert st.summary is not None and st.summary.user_goal == "售后咨询"
    # Facts extracted at compaction: the newest order number mention wins.
    f = await store.get_fact(APP, cid, "order_no")
    assert f is not None and f.source == "compaction" and f.value == "A20261014007"
    # Call parameters: temperature 0, thinking off, JSON mode.
    o = llm.calls[0]
    assert o["overrides"]["temperature"] == 0.0 and o["overrides"]["thinking"] is False
    assert o["kwargs"]["response_format"] == {"type": "json_object"}
    assert "<old_summary>{}</old_summary>" in o["prompt"]
    assert metrics.value("aics_compaction_total", result="success") == 1
    # Below the threshold now → no trigger; load_context uses summary + floor turns.
    assert (await svc.maybe_compact(APP, cid, P, run_mode="eval"))[0] == "none"
    ctx = await svc.load_context(
        ContextQuery(
            app_id=APP, conversation_id=cid, question="q", budget=3000, keep_recent_turns=4
        )
    )
    assert ctx.summary_version == st.version and ctx.recent_turns == 4
    assert ctx.facts["order_no"] == "A20261014007"


async def test_incremental_merge_passes_old_summary() -> None:
    store, cid = conversation(12)
    svc, llm, _ = service(store)
    await svc.compact_now(APP, cid, P)
    for i in range(10):
        store.add_message(APP, cid, "user", f"新问题{i}" + "说明" * 30)
        store.add_message(APP, cid, "assistant", "好的" * 30, answer_type="answer")
    n = len(llm.calls)
    res = await svc.compact_now(APP, cid, P)
    assert res.result == "success" and res.version == 2
    assert '"user_goal": "售后咨询"' in llm.calls[n]["prompt"]  # old summary merged
    assert "第0轮" not in llm.calls[n]["prompt"]  # only new overflow turns


async def test_large_overflow_is_split_into_batches() -> None:
    store, cid = conversation(40, size=200)  # ~ 800 tokens / turn
    svc, _, _ = service(store)
    res = await svc.compact_now(APP, cid, P)
    assert res.result == "success" and res.batches >= 3 and res.version == res.batches
    turns = await store.turns_after(APP, cid, None)
    overflow = overflow_turns(turns, 4)
    assert sum(t.token_count for t in next_batch(overflow)) <= 8000
    assert uncovered_tokens(turns, 4) == sum(t.token_count for t in overflow)


async def test_invalid_output_retried_once_then_succeeds() -> None:
    store, cid = conversation(20)
    seen: list[str] = []

    def responder(prompt: str, ov: dict[str, Any]) -> str:
        seen.append(prompt)
        if "<new_turns>" in prompt and len(seen) == 1:
            return '{"user_goal": 1, "confirmed_info": "not a list"}'
        return default_responder(prompt, ov)

    svc, _, _ = service(store, FakeMemoryLLM(responder))
    res = await svc.compact_now(APP, cid, P)
    assert res.result == "success"
    assert "上一次输出不合格" in seen[1]


async def test_invalid_output_twice_fails_and_changes_nothing() -> None:
    store, cid = conversation(20)
    svc, _, _ = service(store, FakeMemoryLLM(lambda p, o: "这不是 JSON"))
    metrics.reset()
    res = await svc.compact_now(APP, cid, P)
    assert res.result == "failed" and res.error
    st = await store.get_summary_state(APP, cid)
    assert st.version == 0 and st.summary is None
    assert await store.list_facts(APP, cid, include_retracted=True) == []
    assert len(await store.turns_after(APP, cid, None)) == 20  # messages untouched
    assert metrics.value("aics_compaction_failures_total") == 1


async def test_bad_fact_output_fails_whole_batch() -> None:
    store, cid = conversation(20)

    def responder(prompt: str, ov: dict[str, Any]) -> str:
        if "<turns>" in prompt:
            return json.dumps(
                {"facts": [{"key": "password", "value": "x", "source_index": 0, "confidence": 0.9}]}
            )
        return default_responder(prompt, ov)

    svc, _, _ = service(store, FakeMemoryLLM(responder))
    res = await svc.compact_now(APP, cid, P)
    assert res.result == "failed"
    assert (await store.get_summary_state(APP, cid)).version == 0


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectTimeout("t"),
        openai.APIStatusError(
            "rate",
            response=httpx.Response(429, request=httpx.Request("POST", "http://x")),
            body=None,
        ),
    ],
)
async def test_upstream_error_raises_for_retry_and_keeps_state(exc: Exception) -> None:
    store, cid = conversation(20)

    def responder(prompt: str, ov: dict[str, Any]) -> str:
        raise exc

    lock = LocalLock()
    svc, _, _ = service(store, FakeMemoryLLM(responder), lock=lock)
    with pytest.raises(CompactionUpstreamError):
        await svc.compact_now(APP, cid, P)
    assert (await store.get_summary_state(APP, cid)).version == 0
    assert await lock.acquire(cid) is not None  # lock released


async def test_lock_held_skips() -> None:
    store, cid = conversation(20)
    lock = LocalLock()
    await lock.acquire(cid)
    svc, llm, _ = service(store, lock=lock)
    res = await svc.compact_now(APP, cid, P)
    assert res.result == "skipped_lock" and llm.calls == []


async def test_expected_version_mismatch_is_version_conflict() -> None:
    store, cid = conversation(20)
    _, llm, comp = service(store)
    res = await comp.run(APP, cid, P, expected_version=3)
    assert res.result == "version_conflict" and llm.calls == []


async def test_concurrent_writer_bumps_version_mid_flight() -> None:
    """Another compactor (no shared lock) commits while this one waits on the LLM."""
    store, cid = conversation(20)
    llm = FakeMemoryLLM()
    comp = Compactor(in_memory_units(store), llm, LocalLock())
    other = Compactor(in_memory_units(store), FakeMemoryLLM(), LocalLock())
    fired = False

    async def race(prompt: str) -> None:
        nonlocal fired
        if not fired and "<turns>" in prompt:
            fired = True
            assert (await other.run(APP, cid, P)).result == "success"

    llm.before_reply = race
    res = await comp.run(APP, cid, P)
    assert res.result == "version_conflict"
    st = await store.get_summary_state(APP, cid)
    assert st.version == 1  # only the other writer's result
    hist = await store.list_fact_history(APP, cid)
    assert len(hist) == 1  # facts of the losing run were not written


async def test_app_deleting_stops() -> None:
    store, cid = conversation(20)
    store.deleting_apps.add(APP)
    svc, llm, _ = service(store)
    assert (await svc.compact_now(APP, cid, P)).result == "app_deleting" and llm.calls == []


async def test_maybe_compact_modes_and_history_gap_trigger() -> None:
    store, cid = conversation(20)
    queued: list[Any] = []
    svc = MemoryService(in_memory_units(store), enqueue_compaction=lambda *a: queued.append(a))
    trig, _ = await svc.maybe_compact(APP, cid, P, run_mode="online")
    assert trig == "enqueued" and queued[0][:3] == (APP, cid, 0) and queued[0][3] == P
    assert (await svc.maybe_compact(APP, None, P))[0] == "none"
    assert (await MemoryService(in_memory_units(store)).maybe_compact(APP, cid, P))[0] == (
        "no_enqueuer"
    )
    # Small conversation: below threshold unless the turn reported history_gap.
    small, scid = conversation(6, size=1)
    svc2 = MemoryService(in_memory_units(small), enqueue_compaction=lambda *a: queued.append(a))
    high = CompactParams(keep_recent_turns=4, compact_trigger_tokens=6000)
    assert (await svc2.maybe_compact(APP, scid, high))[0] == "none"
    assert (await svc2.maybe_compact(APP, scid, high, flags=["history_gap"]))[0] == "enqueued"
    tiny, tcid = conversation(3, size=1)  # nothing outside the floor → never triggers
    svc3 = MemoryService(in_memory_units(tiny), enqueue_compaction=lambda *a: queued.append(a))
    assert (await svc3.maybe_compact(APP, tcid, high, flags=["history_gap"]))[0] == "none"


async def test_temp_summary_success_and_failure() -> None:
    from aics.memory.context import HistoryMessage

    store = InMemoryConversationStore()
    comp = Compactor(in_memory_units(store), FakeMemoryLLM(), LocalLock())
    s = await comp.temp_summary([HistoryMessage("user", "我要退货")])
    assert s is not None and s["user_goal"] == "售后咨询"
    bad = Compactor(in_memory_units(store), FakeMemoryLLM(lambda p, o: "x"), LocalLock())
    assert await bad.temp_summary([HistoryMessage("user", "我要退货")]) is None


async def test_run_compaction_job_with_runtime() -> None:
    from contextlib import asynccontextmanager

    store, cid = conversation(20)

    @asynccontextmanager
    async def rt() -> Any:
        yield Compactor(in_memory_units(store), FakeMemoryLLM(), LocalLock())

    out = await run_compaction_job(APP, cid, 0, P.model_dump(), runtime=rt)
    assert out == "success"
    assert await run_compaction_job(APP, cid, 0, P.model_dump(), runtime=rt) == "version_conflict"


def test_task_registered_routed_and_retries() -> None:
    from aics.memory.compactor import CompactionUpstreamError as Up
    from aics.tasks import context  # noqa: F401
    from aics.tasks.celery_app import celery_app

    task = celery_app.tasks["aics.compact_conversation"]
    assert celery_app.conf.task_routes["aics.compact_conversation"]["queue"] == "context"
    assert task.max_retries == 3 and Up in task.autoretry_for
    assert "aics.tasks.context" in celery_app.conf.include


def test_enqueue_compaction_sends_to_context(monkeypatch: pytest.MonkeyPatch) -> None:
    from aics.tasks import context

    sent: list[Any] = []
    monkeypatch.setattr(context.celery_app, "send_task", lambda *a, **k: sent.append((a, k)))
    cid = uuid.uuid4()
    context.enqueue_compaction(APP, cid, 2, P)
    (name, kw) = sent[0]
    assert name == ("aics.compact_conversation",) and kw["queue"] == "context"
    assert kw["args"][:3] == [str(APP), str(cid), 2] and kw["args"][3]["keep_recent_turns"] == 4
