"""M4.6 ②：日聚合的真库用例（真 PG；口径见数据库设计 §3.12.1）。

覆盖：上海时区半开日窗、重复聚合幂等、debug/eval 排除、`percentile_cont` 与手算一致、
手工/应答/反馈/转人工各列映射、逐应用与全局视图。
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aics.db.models.conversation import Conversation, Message
from aics.db.models.feedback import Feedback
from aics.db.models.stats import AppDailyStats
from aics.modules.chat.repository import (
    ConversationRepository,
    MessageRepository,
    NewConversation,
    NewMessage,
)
from aics.modules.stats.service import StatsService, day_window, today

pytestmark = pytest.mark.integration

DAY = dt.date(2026, 10, 20)
#: 上海 2026-10-20 10:00 = UTC 02:00
NOON_SH = dt.datetime(2026, 10, 20, 2, 0, tzinfo=dt.UTC)


async def _conversation(
    session: AsyncSession,
    app_id: UUID,
    *,
    channel: str = "api",
    created_at: dt.datetime = NOON_SH,
    handoff: tuple[dt.datetime, dt.datetime] | None = None,
    close_reason: str | None = None,
    closed_at: dt.datetime | None = None,
) -> Conversation:
    conv = await ConversationRepository(session).create(app_id, NewConversation(channel=channel))  # type: ignore[arg-type]
    await session.flush()
    values: dict[str, Any] = {"created_at": created_at, "last_activity_at": created_at}
    if handoff is not None:
        values["handoff_status"] = "accepted"
        values["handoff_requested_at"] = handoff[0]
        values["handoff_accepted_at"] = handoff[1]
    if close_reason is not None:
        values["close_reason"] = close_reason
        values["closed_at"] = closed_at or created_at
        values["status"] = "closed"
    await session.execute(update(Conversation).where(Conversation.id == conv.id).values(**values))
    await session.commit()
    await session.refresh(conv)
    return conv


async def _message(
    session: AsyncSession,
    app_id: UUID,
    conversation_id: UUID,
    *,
    role: str = "assistant",
    created_at: dt.datetime = NOON_SH,
    **kw: Any,
) -> Message:
    data: dict[str, Any] = {"role": role, "content": "内容"}
    data.update(kw)
    message = await MessageRepository(session).append(
        app_id,
        conversation_id,
        NewMessage(**data),  # type: ignore[arg-type]
    )
    await session.execute(
        update(Message).where(Message.id == message.id).values(created_at=created_at)
    )
    await session.commit()
    await session.refresh(message)
    return message


@pytest.fixture
def sessions(pg: dict[str, Any]) -> async_sessionmaker[AsyncSession]:
    return pg["sessions"]


@pytest.fixture
def app_a(pg: dict[str, Any]) -> UUID:
    return pg["app_a"]


async def test_aggregate_maps_every_column(
    sessions: async_sessionmaker[AsyncSession], app_a: UUID
) -> None:
    async with sessions() as session:
        conv = await _conversation(
            session, app_a, handoff=(NOON_SH, NOON_SH + dt.timedelta(seconds=10))
        )
        await _message(session, app_a, conv.id, role="user")
        await _message(session, app_a, conv.id, role="user")
        first = await _message(
            session,
            app_a,
            conv.id,
            answer_type="answer",
            intent="kb_qa",
            prompt_tokens=30,
            completion_tokens=70,
            latency_ms=100,
            first_token_ms=40,
        )
        await _message(
            session,
            app_a,
            conv.id,
            answer_type="answer",
            intent="kb_qa",
            prompt_tokens=20,
            completion_tokens=30,
            latency_ms=300,
        )
        await _message(session, app_a, conv.id, answer_type="refused", intent="out_of_scope")
        await _message(session, app_a, conv.id, role="human_agent", agent_id="ag_1")
        feedback = Feedback(app_id=app_a, message_id=first.id, rating=-1, source="api")
        session.add(feedback)
        await session.flush()
        # 点踩按反馈时间（created_at）落在哪一天计数，与消息时间无关。
        await session.execute(
            update(Feedback).where(Feedback.id == feedback.id).values(created_at=NOON_SH)
        )
        await session.commit()

        row = await StatsService(session).aggregate(app_a, DAY)

    assert row["conversations"] == 1
    assert row["messages"] == 2
    assert row["answered"] == 2
    assert row["refused"] == 1
    assert row["handoff"] == 0
    assert row["handoff_requested"] == 1
    assert row["handoff_accepted"] == 1
    assert row["handoff_accept_p50_s"] == 10
    assert row["human_agent_messages"] == 1
    assert row["negative_feedback"] == 1
    assert row["intent_distribution"] == {"kb_qa": 2, "out_of_scope": 1}
    assert row["tokens_in"] == 50
    assert row["tokens_out"] == 100
    # percentile_cont(0.5) of {100, 300} = 200；(0.95) = 290
    assert row["latency_p50_ms"] == 200
    assert row["latency_p95_ms"] == 290
    assert row["first_token_p95_ms"] == 40


async def test_day_window_is_shanghai_and_half_open(
    sessions: async_sessionmaker[AsyncSession], app_a: UUID
) -> None:
    # 上海 2026-10-19 23:59:59 与 2026-10-20 00:00:00
    before = dt.datetime(2026, 10, 19, 15, 59, 59, tzinfo=dt.UTC)
    after = dt.datetime(2026, 10, 19, 16, 0, 0, tzinfo=dt.UTC)
    async with sessions() as session:
        await _conversation(session, app_a, created_at=before)
        await _conversation(session, app_a, created_at=after)

        yesterday = await StatsService(session).aggregate(app_a, DAY - dt.timedelta(days=1))
        today = await StatsService(session).aggregate(app_a, DAY)

    assert yesterday["conversations"] == 1
    assert today["conversations"] == 1

    start, end = day_window(DAY)
    assert start == dt.datetime(2026, 10, 19, 16, 0, tzinfo=dt.UTC)
    assert end == dt.datetime(2026, 10, 20, 16, 0, tzinfo=dt.UTC)


async def test_aggregate_is_idempotent(
    sessions: async_sessionmaker[AsyncSession], app_a: UUID
) -> None:
    async with sessions() as session:
        conv = await _conversation(session, app_a)
        await _message(session, app_a, conv.id, answer_type="answer", intent="kb_qa")
        service = StatsService(session)
        first = await service.aggregate(app_a, DAY)
        second = await service.aggregate(app_a, DAY)
        count = await session.scalar(
            select(func.count())
            .select_from(AppDailyStats)
            .where(AppDailyStats.app_id == app_a, AppDailyStats.date == DAY)
        )

    assert first == second
    assert count == 1


async def test_debug_and_eval_conversations_are_excluded(
    sessions: async_sessionmaker[AsyncSession], app_a: UUID
) -> None:
    async with sessions() as session:
        for channel in ("api", "debug", "eval"):
            conv = await _conversation(session, app_a, channel=channel)
            await _message(session, app_a, conv.id, role="user")
            await _message(session, app_a, conv.id, answer_type="answer")
        row = await StatsService(session).aggregate(app_a, DAY)

    assert row["conversations"] == 1
    assert row["messages"] == 1
    assert row["answered"] == 1


async def test_idle_and_queue_timeout_families(
    sessions: async_sessionmaker[AsyncSession], app_a: UUID
) -> None:
    async with sessions() as session:
        requested = NOON_SH
        await _conversation(session, app_a, handoff=(requested, requested), close_reason="idle")
        timed_out = await _conversation(session, app_a)
        await session.execute(
            update(Conversation)
            .where(Conversation.id == timed_out.id)
            .values(
                handoff_status="released",
                handoff_end_reason="queue_timeout",
                handoff_requested_at=requested,
                handoff_released_at=requested,
                needs_callback=True,
                callback_reason="queue_timeout",
                callback_requested_at=requested,
            )
        )
        await session.commit()
        row = await StatsService(session).aggregate(app_a, DAY)

    assert row["handoff_queue_timeout"] == 1
    assert row["handoff_requested"] == 2
    assert row["idle_closed"] == 1


async def test_aggregate_apps_covers_every_app(
    pg: dict[str, Any], sessions: async_sessionmaker[AsyncSession]
) -> None:
    async with sessions() as session:
        for app_id in (pg["app_a"], pg["app_b"]):
            conv = await _conversation(session, app_id)
            await _message(session, app_id, conv.id, role="user")
        done = await StatsService(session).aggregate_apps(DAY)
        rows = (
            (await session.execute(select(AppDailyStats.app_id).where(AppDailyStats.date == DAY)))
            .scalars()
            .all()
        )

    assert done == 2
    assert set(rows) == {pg["app_a"], pg["app_b"]}


async def test_series_and_overview_shapes(
    sessions: async_sessionmaker[AsyncSession], app_a: UUID
) -> None:
    async with sessions() as session:
        conv = await _conversation(session, app_a, created_at=dt.datetime.now(dt.UTC))
        await _message(session, app_a, conv.id, role="user", created_at=dt.datetime.now(dt.UTC))
        await _message(
            session,
            app_a,
            conv.id,
            answer_type="answer",
            intent="kb_qa",
            created_at=dt.datetime.now(dt.UTC),
        )
        await StatsService(session).aggregate_apps(today())
        service = StatsService(session)
        series = await service.series(app_a, days=3)
        overview = await service.overview(days=3)

    assert series["days"] == 3
    assert len(series["items"]) == 1
    assert series["items"][0]["messages"] == 1
    assert overview["days"] == 3
    assert overview["totals"]["conversations"] >= 1
    assert overview["intent_distribution"]["kb_qa"] >= 1
    assert any(item["app_id"] == str(app_a) for item in overview["apps"])
