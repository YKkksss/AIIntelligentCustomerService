"""M3.4 ③: the two handoff timeouts (生命周期与转人工 §3.6 / §3.8 / §3.10).

The SQL itself (conditional single-row UPDATEs, `last_activity_at = :seen` comparisons) runs
against a real database in `tests/integration/test_handoff_pg.py`; here the repository is faked
so the orchestration — per-app config, ordering, message writing, lost-race skipping — is
covered without a DB.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from aics.modules.handoff import sweep as sw
from aics.rag.config import HandoffConfig
from tests import events_support

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
APP = uuid.uuid4()
CID = uuid.uuid4()


class _Rows:
    """Enough of a SQLAlchemy `Result` for the two shapes the sweep reads."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _Rows:
        return self

    def all(self) -> list[Any]:
        return list(self._rows)

    def scalar_one(self) -> datetime:
        return NOW  # the injected DB clock


class _Ctx:
    def __init__(self, session: Any) -> None:
        self._session = session

    async def __aenter__(self) -> Any:
        return self._session

    async def __aexit__(self, *exc: object) -> bool:
        return False


class FakeSession:
    def __init__(self, rows: list[Any], state: dict[str, Any]) -> None:
        self._rows, self.state = rows, state

    async def execute(self, stmt: Any) -> _Rows:
        self.state["queries"] += 1
        return _Rows(self._rows)

    async def commit(self) -> None:
        self.state["commits"] += 1


class FakeRepo:
    """Mirrors the conditional-update contract: `*_wins=False` = the row was changed in between."""

    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state

    async def handoff_app_ids_all_apps(self) -> list[uuid.UUID]:
        return list(self.state["app_ids"])

    async def pending_due(self, app_id: uuid.UUID, *, before: datetime, limit: int) -> list[Any]:
        if app_id in self.state["explode_on"]:
            raise RuntimeError("boom")
        self.state["calls"].append(("pending_due", app_id))
        self.state["pending_before"] = before
        return list(self.state["pending"])

    async def accepted_due(self, app_id: uuid.UUID, *, before: datetime, limit: int) -> list[Any]:
        self.state["calls"].append(("accepted_due", app_id))
        self.state["accepted_before"] = before
        return list(self.state["accepted"])

    async def queue_timeout(
        self,
        app_id: uuid.UUID,
        conversation_id: uuid.UUID,
        *,
        seen_requested_at: datetime,
        at: datetime,
    ) -> int:
        self.state["calls"].append(("queue_timeout", conversation_id))
        self.state["seen_requested_at"] = seen_requested_at
        return 1 if self.state["queue_wins"] else 0

    async def close_accepted_idle(
        self,
        app_id: uuid.UUID,
        conversation_id: uuid.UUID,
        *,
        seen_activity: datetime,
        at: datetime,
    ) -> int:
        self.state["calls"].append(("close_accepted_idle", conversation_id))
        self.state["seen_activity"] = seen_activity
        return 1 if self.state["idle_wins"] else 0

    async def get(self, app_id: uuid.UUID, conversation_id: uuid.UUID) -> SimpleNamespace | None:
        return _conv(app_id, conversation_id)


def _conv(app_id: uuid.UUID, conversation_id: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(
        id=conversation_id,
        app_id=app_id,
        channel="api",
        external_user_id="u_1",
        handoff_agent_id="ag_1",
    )


def _row(
    app_id: uuid.UUID,
    handoff_config: dict[str, Any] | None = None,
    webhook_config: dict[str, Any] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=app_id,
        handoff_config=handoff_config,
        webhook_config=webhook_config,
        status="active",
        deleted_at=None,
    )


def rig(
    monkeypatch: pytest.MonkeyPatch,
    *,
    app_ids: list[uuid.UUID] | None = None,
    apps: list[Any] | None = None,
    pending: list[tuple[uuid.UUID, datetime]] | None = None,
    accepted: list[tuple[uuid.UUID, datetime]] | None = None,
    queue_wins: bool = True,
    idle_wins: bool = True,
    handoff_config: dict[str, Any] | None = None,
    webhook_config: dict[str, Any] | None = None,
    explode_on: tuple[uuid.UUID, ...] = (),
) -> dict[str, Any]:
    ids = list(app_ids) if app_ids is not None else [APP]
    rows = apps if apps is not None else [_row(a, handoff_config, webhook_config) for a in ids]
    state: dict[str, Any] = {
        "app_ids": ids,
        "pending": list(pending or []),
        "accepted": list(accepted or []),
        "queue_wins": queue_wins,
        "idle_wins": idle_wins,
        "explode_on": set(explode_on),
        "calls": [],
        "messages": [],
        "commits": 0,
        "queries": 0,
        "pending_before": None,
        "accepted_before": None,
        "seen_requested_at": None,
        "seen_activity": None,
    }

    def sessions() -> _Ctx:
        return _Ctx(FakeSession(rows, state))

    async def fake_append(
        session: Any,
        app_id: uuid.UUID,
        conversation_id: uuid.UUID,
        *,
        role: str,
        content: str,
        kind: str = "chat",
        agent_id: str | None = None,
        message_id: uuid.UUID | None = None,
    ) -> SimpleNamespace:
        state["messages"].append(
            {
                "app_id": app_id,
                "conversation_id": conversation_id,
                "role": role,
                "kind": kind,
                "content": content,
            }
        )
        return SimpleNamespace(
            id=uuid.uuid4(),
            role=role,
            kind=kind,
            content=content,
            agent_id=agent_id,
            answer_type=None,
            created_at=NOW,
        )

    monkeypatch.setattr(sw, "HandoffRepository", lambda session: FakeRepo(state))
    monkeypatch.setattr(sw, "append_agent_message", fake_append)
    state["events"] = events_support.install(monkeypatch)
    state["sessions"] = sessions
    return state


# ------------------------------------------------------------------ config / result plumbing
def test_config_of_defaults_and_merges_and_falls_back() -> None:
    assert sw._config_of(_row(APP)) == HandoffConfig()
    merged = sw._config_of(_row(APP, {"handoff_queue_timeout_minutes": 9}))
    assert merged.handoff_queue_timeout_minutes == 9
    assert merged.queue_timeout_text == HandoffConfig().queue_timeout_text
    # 坏配置回落到默认值而不是把整轮扫描炸掉（与 chat.lifecycle 同一策略）
    assert sw._config_of(_row(APP, {"handoff_queue_timeout_minutes": 0})) == HandoffConfig()
    assert sw._config_of(_row(APP, {"handoff_ai_mode": "bogus"})) == HandoffConfig()


def test_sweep_result_dict() -> None:
    r = sw.HandoffSweepResult(apps=1, queue_timeouts=2, accepted_idle_closed=3, skipped_apps=["x"])
    assert r.as_dict() == {
        "apps": 1,
        "queue_timeouts": 2,
        "accepted_idle_closed": 3,
        "skipped_apps": ["x"],
    }


# ------------------------------------------------------------------ §3.6 queue timeout
def test_queue_timeout_releases_and_writes_the_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = NOW - timedelta(minutes=6)
    st = rig(monkeypatch, pending=[(CID, seen)])
    res = asyncio.run(sw.sweep(st["sessions"], now=NOW))
    assert (res.apps, res.queue_timeouts, res.accepted_idle_closed) == (1, 1, 0)
    assert st["seen_requested_at"] == seen  # 条件更新比对的正是读到的值
    (msg,) = st["messages"]
    assert msg["role"] == "system" and msg["kind"] == "handoff_queue_timeout"
    assert msg["content"] == HandoffConfig().queue_timeout_text
    assert msg["conversation_id"] == CID and st["commits"] == 1
    # M3.5：先这条系统消息，再排队超时事件（顺序 = 发生顺序，§7.3）
    assert st["events"].types() == ["message.created", "handoff.queue_timeout"]
    timeout = st["events"].of("handoff.queue_timeout")[0].data
    assert timeout["waited_seconds"] == 360 and timeout["needs_callback"] is True
    assert timeout["message"]["kind"] == "handoff_queue_timeout"


def test_queue_timeout_uses_the_app_window(monkeypatch: pytest.MonkeyPatch) -> None:
    st = rig(
        monkeypatch,
        handoff_config={"handoff_queue_timeout_minutes": 9, "handoff_accepted_idle_minutes": 45},
    )
    asyncio.run(sw.sweep(st["sessions"], now=NOW))
    assert st["pending_before"] == NOW - timedelta(minutes=9)
    assert st["accepted_before"] == NOW - timedelta(minutes=45)


def test_lost_race_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """读完之后被坐席接入 / 用户又说了话：条件更新影响 0 行——不写消息、不提交。"""
    st = rig(
        monkeypatch,
        pending=[(CID, NOW - timedelta(minutes=6))],
        accepted=[(CID, NOW - timedelta(minutes=40))],
        queue_wins=False,
        idle_wins=False,
    )
    res = asyncio.run(sw.sweep(st["sessions"], now=NOW))
    assert (res.queue_timeouts, res.accepted_idle_closed) == (0, 0)
    assert st["messages"] == [] and st["commits"] == 0
    assert st["events"].events == []  # 抢输就不发事件


# ------------------------------------------------------------------ §3.8 accepted idle
def test_accepted_idle_closes_with_an_empty_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = NOW - timedelta(minutes=31)
    st = rig(monkeypatch, accepted=[(CID, seen)])
    res = asyncio.run(sw.sweep(st["sessions"], now=NOW))
    assert (res.queue_timeouts, res.accepted_idle_closed) == (0, 1)
    assert st["seen_activity"] == seen
    (msg,) = st["messages"]
    assert msg["role"] == "system" and msg["kind"] == "handoff_idle_closed"
    assert msg["content"] == ""  # 空内容时间线标记（§3.8）
    assert st["commits"] == 1
    assert st["events"].types() == [
        "message.created",
        "handoff.released",
        "conversation.closed",
    ]
    released = st["events"].of("handoff.released")[0].data
    assert (released["released_by"], released["reason"], released["agent_id"]) == (
        "system",
        "accepted_idle",
        "ag_1",
    )
    assert st["events"].of("conversation.closed")[0].data == {"reason": "handoff_idle"}


def test_queue_timeouts_run_before_accepted_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    st = rig(monkeypatch, pending=[(CID, NOW)], accepted=[(CID, NOW)])
    asyncio.run(sw.sweep(st["sessions"], now=NOW))
    calls = [c[0] for c in st["calls"]]
    assert calls.index("queue_timeout") < calls.index("close_accepted_idle")


# ------------------------------------------------------------------ apps, ordering, clock
def test_apps_without_a_row_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    other = uuid.uuid4()
    st = rig(monkeypatch, app_ids=[APP, other], apps=[_row(APP)])
    res = asyncio.run(sw.sweep(st["sessions"], now=NOW))
    assert res.apps == 1 and res.skipped_apps == [str(other)]


def test_one_broken_app_does_not_stop_the_others(monkeypatch: pytest.MonkeyPatch) -> None:
    other = uuid.uuid4()
    st = rig(
        monkeypatch,
        app_ids=[APP, other],
        pending=[(CID, NOW - timedelta(minutes=6))],
        explode_on=(APP,),
    )
    res = asyncio.run(sw.sweep(st["sessions"], now=NOW))
    assert (res.apps, res.queue_timeouts) == (2, 1)  # APP 抛错被跳过，另一个应用照常处理


def test_sweep_defaults_to_the_database_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    st = rig(monkeypatch)
    res = asyncio.run(sw.sweep(st["sessions"]))
    assert st["pending_before"] == NOW - timedelta(minutes=5)  # 时钟取自数据库（_db_now）
    assert res.apps == 1
