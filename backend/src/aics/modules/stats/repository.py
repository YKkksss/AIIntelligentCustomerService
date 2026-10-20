"""每日统计的存取（M4.6 ②，数据库设计 §3.12.1）。

写入只有一条路径：`upsert`（`INSERT … ON CONFLICT (app_id, date) DO UPDATE`，重复执行结果一致）。
读取有两条：应用内按日序列 `series`，平台视角的整表行 `rows_all_apps`（只给超级管理员的仪表盘用）。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from aics.db.models.app import App
from aics.db.models.stats import AppDailyStats
from aics.db.scoped import AppScopedRepository

#: 不可由聚合结果覆盖的主键列。
_KEYS = ("app_id", "date")


class StatsRepository(AppScopedRepository[AppDailyStats]):
    model = AppDailyStats

    async def upsert(self, app_id: UUID, values: Mapping[str, Any]) -> None:
        """写入或覆盖一天的数据；`updated_at` 由数据库刷新，调用方负责 `commit`。"""
        payload = dict(values)
        if payload.get("app_id") != app_id:
            raise ValueError("upsert 的值与应用不一致")
        stmt = pg_insert(AppDailyStats).values(**payload)
        updates = {name: stmt.excluded[name] for name in payload if name not in _KEYS}
        await self.session.execute(
            stmt.on_conflict_do_update(index_elements=list(_KEYS), set_=updates)
        )

    async def series(self, app_id: UUID, *, since: dt.date, until: dt.date) -> list[AppDailyStats]:
        """按日期升序返回 `[since, until]` 内的行（没有数据的日子不补零）。"""
        stmt = (
            self._scoped(app_id)
            .where(AppDailyStats.date >= since, AppDailyStats.date <= until)
            .order_by(AppDailyStats.date)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def rows_all_apps(
        self, *, since: dt.date, until: dt.date
    ) -> list[tuple[AppDailyStats, str]]:
        """平台视角：`[since, until]` 内所有应用的每日行（含应用名），供仪表盘汇总。"""
        stmt = (
            select(AppDailyStats, App.name)
            .join(App, App.id == AppDailyStats.app_id)
            .where(
                AppDailyStats.date >= since,
                AppDailyStats.date <= until,
                App.deleted_at.is_(None),
            )
            .order_by(AppDailyStats.date, App.name)
        )
        return [(row, name) for row, name in (await self.session.execute(stmt)).all()]

    async def app_ids_all_apps(self) -> list[UUID]:
        """平台视角：全部未删除的应用 id（聚合任务逐应用重算）。"""
        stmt = select(App.id).where(App.deleted_at.is_(None)).order_by(App.id)
        return list((await self.session.execute(stmt)).scalars())
