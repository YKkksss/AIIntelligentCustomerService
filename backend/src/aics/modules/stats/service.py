"""统计服务（M4.6 ②，数据库设计 §3.12.1）。

口径写死在这里，并由 `tests/integration/test_stats_pg.py` 守住：

- 只统计 `channel IN ('api', 'openai')`；`debug` / `eval` 是内部流量，不参与。
- 自然日按 `Asia/Shanghai`；窗口是 `[当日 00:00, 次日 00:00)`（半开区间，跨零点不重不漏）。
- 分位数用 `percentile_cont`（只对非 NULL 值计算）；`first_token_p95_ms` 只覆盖流式轮次
  （ISSUE-93），没有样本时为 NULL。
- 聚合结果 `INSERT … ON CONFLICT (app_id, date) DO UPDATE`，重复执行结果一致。
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aics.db.models.conversation import Conversation, Message
from aics.db.models.feedback import Feedback
from aics.db.models.stats import STATS_CHANNELS, AppDailyStats
from aics.modules.stats.repository import StatsRepository

#: 统计口径的时区（与业务时区一致，不跟随容器本地时区）。
STATS_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_DAYS = 7
MAX_DAYS = 90
#: 聚合任务的批量（逐应用执行；应用很多时也只按 id 顺序跑）。
BATCH = 200


def today() -> dt.date:
    """统计口径下的「今天」。"""
    return dt.datetime.now(STATS_TZ).date()


def day_window(day: dt.date, *, tz: ZoneInfo = STATS_TZ) -> tuple[dt.datetime, dt.datetime]:
    """`day` 在 `tz` 下的半开区间 `[00:00, 次日 00:00)`。"""
    start = dt.datetime.combine(day, dt.time.min, tzinfo=tz)
    return start, start + dt.timedelta(days=1)


def _percentile(column: sa.ColumnElement[Any], q: float) -> sa.ColumnElement[Any]:
    return func.percentile_cont(q).within_group(column.asc())


def _round(value: Any) -> int | None:
    """`percentile_cont` 返回 float / Decimal；没有样本时是 NULL。"""
    return None if value is None else int(round(float(value)))


class StatsService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = StatsRepository(session)

    # ---- 聚合 ----
    async def aggregate(self, app_id: UUID, day: dt.date) -> dict[str, Any]:
        """重算 `app_id` 在 `day` 的一行统计并写入，返回该行（重复调用结果一致）。"""
        start, end = day_window(day)
        channel_ok = Conversation.channel.in_(STATS_CHANNELS)
        message_window = (
            Message.app_id == app_id,
            Message.created_at >= start,
            Message.created_at < end,
            channel_ok,
        )
        assistant = Message.role == "assistant"

        # 会话：新建数 + 三个转人工时间点 + 两种结束原因 + 空闲关闭。
        conv = (
            await self.session.execute(
                select(
                    func.count().filter(
                        Conversation.created_at >= start, Conversation.created_at < end
                    ),
                    func.count().filter(
                        Conversation.handoff_requested_at >= start,
                        Conversation.handoff_requested_at < end,
                    ),
                    func.count().filter(
                        Conversation.handoff_accepted_at >= start,
                        Conversation.handoff_accepted_at < end,
                    ),
                    func.count().filter(
                        Conversation.handoff_released_at >= start,
                        Conversation.handoff_released_at < end,
                    ),
                    func.count().filter(
                        Conversation.handoff_end_reason == "queue_timeout",
                        Conversation.handoff_requested_at >= start,
                        Conversation.handoff_requested_at < end,
                    ),
                    func.count().filter(
                        Conversation.handoff_end_reason == "accepted_idle",
                        Conversation.handoff_requested_at >= start,
                        Conversation.handoff_requested_at < end,
                    ),
                    func.count().filter(
                        Conversation.callback_reason == "offline",
                        Conversation.callback_requested_at >= start,
                        Conversation.callback_requested_at < end,
                    ),
                    func.count().filter(
                        Conversation.close_reason == "idle",
                        Conversation.closed_at >= start,
                        Conversation.closed_at < end,
                    ),
                ).where(Conversation.app_id == app_id, channel_ok)
            )
        ).one()

        # 消息：user 轮数、assistant 的 answer_type 分布、人工坐席条数。
        roles = (
            await self.session.execute(
                select(Message.role, Message.answer_type, func.count())
                .select_from(Message)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(*message_window)
                .group_by(Message.role, Message.answer_type)
            )
        ).all()
        messages = sum(count for role, _, count in roles if role == "user")
        human_agent_messages = sum(count for role, _, count in roles if role == "human_agent")
        by_type = {atype: count for role, atype, count in roles if role == "assistant" and atype}

        # 意图分布（assistant 上才有）。
        intents = (
            await self.session.execute(
                select(Message.intent, func.count())
                .select_from(Message)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(*message_window, assistant, Message.intent.is_not(None))
                .group_by(Message.intent)
                .order_by(Message.intent)
            )
        ).all()

        # tokens 与延迟分位数。
        usage = (
            await self.session.execute(
                select(
                    func.coalesce(func.sum(Message.prompt_tokens), 0),
                    func.coalesce(func.sum(Message.completion_tokens), 0),
                    _percentile(Message.latency_ms, 0.5),
                    _percentile(Message.latency_ms, 0.95),
                    _percentile(Message.first_token_ms, 0.95),
                )
                .select_from(Message)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(*message_window, assistant)
            )
        ).one()

        # pending → accepted 的耗时中位数（秒）。
        accept_p50 = (
            await self.session.execute(
                select(
                    _percentile(
                        func.extract(
                            "epoch",
                            Conversation.handoff_accepted_at - Conversation.handoff_requested_at,
                        ),
                        0.5,
                    )
                ).where(
                    Conversation.app_id == app_id,
                    channel_ok,
                    Conversation.handoff_accepted_at >= start,
                    Conversation.handoff_accepted_at < end,
                    Conversation.handoff_requested_at.is_not(None),
                )
            )
        ).scalar()

        # 点踩：只算参与统计的渠道（反馈挂在消息上）。
        negative_feedback = int(
            (
                await self.session.execute(
                    select(func.count())
                    .select_from(Feedback)
                    .join(Message, Message.id == Feedback.message_id)
                    .join(Conversation, Conversation.id == Message.conversation_id)
                    .where(
                        Feedback.app_id == app_id,
                        Feedback.rating == -1,
                        Feedback.created_at >= start,
                        Feedback.created_at < end,
                        channel_ok,
                    )
                )
            ).scalar_one()
        )

        values: dict[str, Any] = {
            "app_id": app_id,
            "date": day,
            "conversations": int(conv[0]),
            "messages": int(messages),
            "answered": int(by_type.get("answer", 0)),
            "refused": int(by_type.get("refused", 0)),
            "handoff": int(by_type.get("handoff", 0)),
            "errors": int(by_type.get("error", 0)),
            "handoff_requested": int(conv[1]),
            "handoff_accepted": int(conv[2]),
            "handoff_released": int(conv[3]),
            "handoff_accept_p50_s": _round(accept_p50),
            "handoff_queue_timeout": int(conv[4]),
            "handoff_offline": int(conv[6]),
            "handoff_accepted_idle_closed": int(conv[5]),
            "human_agent_messages": int(human_agent_messages),
            "idle_closed": int(conv[7]),
            "negative_feedback": negative_feedback,
            "intent_distribution": {intent: int(count) for intent, count in intents},
            "tokens_in": int(usage[0]),
            "tokens_out": int(usage[1]),
            "latency_p50_ms": _round(usage[2]),
            "latency_p95_ms": _round(usage[3]),
            "first_token_p95_ms": _round(usage[4]),
        }
        await self.repo.upsert(app_id, values)
        await self.session.commit()
        return {**values, "app_id": str(app_id), "date": day.isoformat()}

    async def aggregate_apps(self, day: dt.date, *, app_ids: list[UUID] | None = None) -> int:
        """重算每个应用在 `day` 的一行；返回成功应用数（单个失败不拖垮整批）。"""
        targets = app_ids if app_ids is not None else await self.repo.app_ids_all_apps()
        done = 0
        for app_id in targets[:BATCH]:
            await self.aggregate(app_id, day)
            done += 1
        return done

    # ---- 查询 ----
    async def series(self, app_id: UUID, *, days: int = DEFAULT_DAYS) -> dict[str, Any]:
        """应用内按日序列（升序；缺数据的日子不补零，由前端按日期对齐）。"""
        span = min(max(days, 1), MAX_DAYS)
        until = today()
        since = until - dt.timedelta(days=span - 1)
        rows = await self.repo.series(app_id, since=since, until=until)
        return {
            "app_id": str(app_id),
            "days": span,
            "since": since.isoformat(),
            "until": until.isoformat(),
            "items": [_row(row) for row in rows],
        }

    async def overview(self, *, days: int = DEFAULT_DAYS) -> dict[str, Any]:
        """平台视角：窗口内逐日序列 + 逐应用合计 + 总合计。"""
        span = min(max(days, 1), MAX_DAYS)
        until = today()
        since = until - dt.timedelta(days=span - 1)
        rows = await self.repo.rows_all_apps(since=since, until=until)

        daily: dict[str, dict[str, int]] = {}
        by_app: dict[UUID, dict[str, Any]] = {}
        totals = {name: 0 for name in _SUM_FIELDS}
        intents: dict[str, int] = {}
        for row, name in rows:
            bucket = daily.setdefault(row.date.isoformat(), {name: 0 for name in _SUM_FIELDS})
            for field in _SUM_FIELDS:
                value = int(getattr(row, field))
                bucket[field] += value
                totals[field] += value
            app = by_app.setdefault(
                row.app_id,
                {"app_id": str(row.app_id), "name": name, **{f: 0 for f in _SUM_FIELDS}},
            )
            for field in _SUM_FIELDS:
                app[field] += int(getattr(row, field))
            for intent, count in (row.intent_distribution or {}).items():
                intents[intent] = intents.get(intent, 0) + int(count)

        return {
            "days": span,
            "since": since.isoformat(),
            "until": until.isoformat(),
            "totals": totals,
            "apps": sorted(by_app.values(), key=lambda item: str(item["name"])),
            "series": [{"date": date, **bucket} for date, bucket in sorted(daily.items())],
            "intent_distribution": dict(sorted(intents.items())),
        }


#: 仪表盘合计口径只累加计数列；分位数按日给出，不做跨日汇总（数据库设计 §3.12.1）。
_SUM_FIELDS = (
    "conversations",
    "messages",
    "answered",
    "refused",
    "handoff",
    "errors",
    "handoff_requested",
    "handoff_accepted",
    "handoff_released",
    "handoff_queue_timeout",
    "handoff_offline",
    "handoff_accepted_idle_closed",
    "human_agent_messages",
    "idle_closed",
    "negative_feedback",
    "tokens_in",
    "tokens_out",
)


async def run_aggregate_job(
    day: str | None = None, *, yesterday: bool = False, settings: Any = None
) -> dict[str, Any]:
    """Celery 任务体：独立事件循环，用 NullPool 引擎（asyncio.run 每任务一个 loop）。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from aics.core.config import get_settings

    s = settings or get_settings()
    if day:
        target = dt.date.fromisoformat(day)
    elif yesterday:
        target = today() - dt.timedelta(days=1)
    else:
        target = today()
    engine = create_async_engine(s.db_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            done = await StatsService(session).aggregate_apps(target)
    finally:
        await engine.dispose()
    return {"date": target.isoformat(), "apps": done}


def _row(row: AppDailyStats) -> dict[str, Any]:
    return {
        "date": row.date.isoformat(),
        **{name: int(getattr(row, name)) for name in _SUM_FIELDS},
        "handoff_accept_p50_s": row.handoff_accept_p50_s,
        "latency_p50_ms": row.latency_p50_ms,
        "latency_p95_ms": row.latency_p95_ms,
        "first_token_p95_ms": row.first_token_p95_ms,
        "intent_distribution": dict(row.intent_distribution or {}),
    }
