"""M4.6 ②：统计聚合与查询口径（纯单测，不连库）。

口径是这一步的核心风险：跨零点边界、渠道过滤、空样本的分位数、意图分布与幂等。真实 SQL 的
正确性由集成用例 `tests/integration/test_stats_pg.py` 守；这里守服务层的映射与边界。
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from aics.modules.stats.repository import StatsRepository
from aics.modules.stats.service import (
    MAX_DAYS,
    STATS_TZ,
    StatsService,
    day_window,
    today,
)

APP_ID = uuid4()


class FakeResult:
    def __init__(
        self,
        *,
        one: Any = None,
        all: Any = None,
        scalar: Any = None,
        scalar_one: Any = None,
        scalars: Any = None,
    ) -> None:
        self._one = one
        self._all = all
        self._scalar = scalar
        self._scalar_one = scalar_one
        self._scalars = scalars or []

    def one(self) -> Any:
        return self._one

    def all(self) -> Any:
        return self._all or []

    def scalar(self) -> Any:
        return self._scalar

    def scalar_one(self) -> Any:
        return self._scalar_one

    def scalars(self) -> Any:
        return self._scalars


class FakeSession:
    """按调用顺序返回预置结果；`execute` 的语句内容不参与断言（集成用例负责）。"""

    def __init__(self, results: list[FakeResult]) -> None:
        self._results = list(results)
        self.commits = 0

    async def execute(self, stmt: Any) -> FakeResult:
        return self._results.pop(0)

    async def commit(self) -> None:
        self.commits += 1


def conv_row(
    *,
    conversations: int = 5,
    requested: int = 2,
    accepted: int = 1,
    released: int = 1,
    queue_timeout: int = 1,
    accepted_idle: int = 0,
    offline: int = 1,
    idle_closed: int = 3,
) -> FakeResult:
    return FakeResult(
        one=(
            conversations,
            requested,
            accepted,
            released,
            queue_timeout,
            accepted_idle,
            offline,
            idle_closed,
        )
    )


def roles_result(rows: list[tuple[str, str | None, int]] | None = None) -> FakeResult:
    return FakeResult(
        all=rows
        or [
            ("user", None, 10),
            ("assistant", "answer", 8),
            ("assistant", "refused", 1),
            ("assistant", "handoff", 1),
            ("human_agent", None, 3),
        ]
    )


def intents_result(rows: list[tuple[str, int]] | None = None) -> FakeResult:
    return FakeResult(all=rows or [("kb_qa", 6), ("chitchat", 2), ("human", 1)])


def usage_result(
    tokens_in: int = 1000,
    tokens_out: int = 2000,
    p50: float | None = 120.4,
    p95: float | None = 450.6,
    fp95: float | None = None,
) -> FakeResult:
    return FakeResult(one=(tokens_in, tokens_out, p50, p95, fp95))


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    async def fake_upsert(self: StatsRepository, app_id: Any, values: Any) -> None:
        rows.append(dict(values))

    monkeypatch.setattr(StatsRepository, "upsert", fake_upsert)
    return rows


def test_day_window_is_shanghai_midnight_half_open() -> None:
    start, end = day_window(dt.date(2026, 10, 20))
    assert start == dt.datetime(2026, 10, 20, 0, 0, tzinfo=STATS_TZ)
    assert end - start == dt.timedelta(days=1)
    assert start.utcoffset() == dt.timedelta(hours=8)
    assert today() == dt.datetime.now(STATS_TZ).date()


async def test_aggregate_maps_every_column(captured: list[dict[str, Any]]) -> None:
    session = FakeSession(
        [
            conv_row(),
            roles_result(),
            intents_result(),
            usage_result(),
            FakeResult(scalar=42.6),
            FakeResult(scalar_one=2),
        ]
    )
    day = dt.date(2026, 10, 20)
    row = await StatsService(session).aggregate(APP_ID, day)

    assert session.commits == 1
    values = captured[0]
    assert values["app_id"] == APP_ID
    assert values["date"] == day
    assert values["conversations"] == 5
    assert values["messages"] == 10
    assert values["answered"] == 8
    assert values["refused"] == 1
    assert values["handoff"] == 1
    assert values["errors"] == 0
    assert values["handoff_requested"] == 2
    assert values["handoff_accepted"] == 1
    assert values["handoff_released"] == 1
    assert values["handoff_accept_p50_s"] == 43
    assert values["handoff_queue_timeout"] == 1
    assert values["handoff_offline"] == 1
    assert values["handoff_accepted_idle_closed"] == 0
    assert values["human_agent_messages"] == 3
    assert values["idle_closed"] == 3
    assert values["negative_feedback"] == 2
    assert values["intent_distribution"] == {"kb_qa": 6, "chitchat": 2, "human": 1}
    assert values["tokens_in"] == 1000
    assert values["tokens_out"] == 2000
    assert values["latency_p50_ms"] == 120
    assert values["latency_p95_ms"] == 451
    assert values["first_token_p95_ms"] is None
    # 返回体把 UUID / date 转成字符串，接口层再用 response_model 序列化
    assert row["app_id"] == str(APP_ID)
    assert row["date"] == "2026-10-20"


async def test_aggregate_keeps_nulls_for_empty_percentiles(
    captured: list[dict[str, Any]],
) -> None:
    session = FakeSession(
        [
            conv_row(conversations=0, requested=0, accepted=0, released=0),
            FakeResult(all=[]),
            FakeResult(all=[]),
            usage_result(0, 0, None, None, None),
            FakeResult(scalar=None),
            FakeResult(scalar_one=0),
        ]
    )
    await StatsService(session).aggregate(APP_ID, dt.date(2026, 10, 20))
    values = captured[0]
    assert values["conversations"] == 0
    assert values["intent_distribution"] == {}
    assert values["latency_p50_ms"] is None
    assert values["latency_p95_ms"] is None
    assert values["handoff_accept_p50_s"] is None


async def test_aggregate_apps_walks_every_app(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, dt.date]] = []

    async def fake_aggregate(self: StatsService, app_id: Any, day: dt.date) -> dict[str, Any]:
        calls.append((app_id, day))
        return {}

    async def fake_ids(self: StatsRepository) -> list[Any]:
        return [uuid4(), uuid4()]

    monkeypatch.setattr(StatsService, "aggregate", fake_aggregate)
    monkeypatch.setattr(StatsRepository, "app_ids_all_apps", fake_ids)
    done = await StatsService(FakeSession([])).aggregate_apps(dt.date(2026, 10, 20))
    assert done == 2
    assert len(calls) == 2


def _row(day: str, **over: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "app_id": uuid4(),
        "date": dt.date.fromisoformat(day),
        "conversations": 1,
        "messages": 2,
        "answered": 2,
        "refused": 0,
        "handoff": 0,
        "errors": 0,
        "handoff_requested": 0,
        "handoff_accepted": 0,
        "handoff_released": 0,
        "handoff_queue_timeout": 0,
        "handoff_offline": 0,
        "handoff_accepted_idle_closed": 0,
        "human_agent_messages": 0,
        "idle_closed": 0,
        "negative_feedback": 1,
        "tokens_in": 10,
        "tokens_out": 20,
        "handoff_accept_p50_s": None,
        "latency_p50_ms": 100,
        "latency_p95_ms": 200,
        "first_token_p95_ms": None,
        "intent_distribution": {"kb_qa": 1},
    }
    base.update(over)
    return SimpleNamespace(**base)


async def test_series_returns_date_ascending_items() -> None:
    session = FakeSession([FakeResult(scalars=[_row("2026-10-19"), _row("2026-10-20")])])
    body = await StatsService(session).series(APP_ID, days=2)
    assert body["app_id"] == str(APP_ID)
    assert body["days"] == 2
    assert [item["date"] for item in body["items"]] == ["2026-10-19", "2026-10-20"]
    assert body["items"][0]["intent_distribution"] == {"kb_qa": 1}


async def test_series_clamps_days() -> None:
    body = await StatsService(FakeSession([FakeResult(scalars=[])])).series(APP_ID, days=9999)
    assert body["days"] == MAX_DAYS
    body = await StatsService(FakeSession([FakeResult(scalars=[])])).series(APP_ID, days=0)
    assert body["days"] == 1


async def test_overview_sums_counts_and_keeps_daily_series() -> None:
    rows = [
        (_row("2026-10-19", conversations=2, app_id=APP_ID), "客服 A"),
        (_row("2026-10-19", conversations=1, intent_distribution={"chitchat": 4}), "客服 B"),
        (_row("2026-10-20", conversations=3, app_id=APP_ID), "客服 A"),
    ]
    body = await StatsService(FakeSession([FakeResult(all=rows)])).overview(days=2)

    assert body["days"] == 2
    assert body["totals"]["conversations"] == 6
    assert body["totals"]["negative_feedback"] == 3
    assert [item["date"] for item in body["series"]] == ["2026-10-19", "2026-10-20"]
    assert body["series"][0]["conversations"] == 3
    names = {app["name"]: app for app in body["apps"]}
    assert set(names) == {"客服 A", "客服 B"}
    assert names["客服 A"]["conversations"] == 5
    assert body["intent_distribution"] == {"chitchat": 4, "kb_qa": 2}
