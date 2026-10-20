"""统计接口（M4.6 ②）：全局仪表盘与应用内统计。

- `GET /api/v1/admin/stats/overview?days=`：平台级（只有超级管理员能过 `require_perm` 的
  app_id=None 分支），返回逐日序列、逐应用合计与总合计。
- `GET /api/v1/admin/apps/{app_id}/stats?days=`：应用内按日序列，权限 `stats.read`
  （viewer 也有——统计是只读的）。

两条接口都只读 `app_daily_stats`，不触发聚合；聚合由 beat 任务每小时重算当天、00:30 定稿前一天。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from aics.core.openapi import errors
from aics.core.security import AuthedUser, require_perm
from aics.modules.stats.service import DEFAULT_DAYS, MAX_DAYS, StatsService

router = APIRouter(tags=["admin-stats"])

StatsUser = Annotated[AuthedUser, Depends(require_perm("stats.read"))]

STATS_ERRORS = errors(401, 403, 422, 429)


class DailyRow(BaseModel):
    date: str
    conversations: int
    messages: int
    answered: int
    refused: int
    handoff: int
    errors: int
    handoff_requested: int
    handoff_accepted: int
    handoff_released: int
    handoff_queue_timeout: int
    handoff_offline: int
    handoff_accepted_idle_closed: int
    human_agent_messages: int
    idle_closed: int
    negative_feedback: int
    tokens_in: int
    tokens_out: int
    handoff_accept_p50_s: int | None = None
    latency_p50_ms: int | None = None
    latency_p95_ms: int | None = None
    first_token_p95_ms: int | None = None
    intent_distribution: dict[str, int]


class AppStats(BaseModel):
    app_id: str
    days: int
    since: str
    until: str
    items: list[DailyRow]


class TotalsBody(BaseModel):
    conversations: int
    messages: int
    answered: int
    refused: int
    handoff: int
    errors: int
    handoff_requested: int
    handoff_accepted: int
    handoff_released: int
    handoff_queue_timeout: int
    handoff_offline: int
    handoff_accepted_idle_closed: int
    human_agent_messages: int
    idle_closed: int
    negative_feedback: int
    tokens_in: int
    tokens_out: int


class DailyTotals(TotalsBody):
    date: str
    conversations: int
    messages: int
    answered: int
    refused: int
    handoff: int
    errors: int
    handoff_requested: int
    handoff_accepted: int
    handoff_released: int
    handoff_queue_timeout: int
    handoff_offline: int
    handoff_accepted_idle_closed: int
    human_agent_messages: int
    idle_closed: int
    negative_feedback: int
    tokens_in: int
    tokens_out: int


class AppTotals(TotalsBody):
    app_id: str
    name: str


class OverviewBody(BaseModel):
    days: int
    since: str
    until: str
    totals: TotalsBody
    apps: list[AppTotals]
    series: list[DailyTotals]
    intent_distribution: dict[str, int]


def _runtime(request: Request) -> Any:
    return request.app.state.stats_runtime


async def _service(request: Request) -> AsyncIterator[StatsService]:
    runtime = _runtime(request)
    async with runtime.session() as session:
        yield runtime.service(session)


ServiceDep = Annotated[StatsService, Depends(_service)]
DaysQuery = Annotated[int, Query(ge=1, le=MAX_DAYS, description="自然日窗口（含今天）")]


@router.get(
    "/api/v1/admin/stats/overview",
    summary="全局仪表盘（按日序列、逐应用合计与总合计）",
    response_model=OverviewBody,
    responses=STATS_ERRORS,
)
async def stats_overview(
    request: Request,
    user: StatsUser,
    service: ServiceDep,
    days: DaysQuery = DEFAULT_DAYS,
) -> dict[str, Any]:
    # 返回 dict 让 FastAPI 按响应模型校验并过滤多余键（不再返回 JSONResponse 绕过校验）。
    return await service.overview(days=days)


@router.get(
    "/api/v1/admin/apps/{app_id}/stats",
    summary="应用内统计（按日序列）",
    response_model=AppStats,
    responses=STATS_ERRORS,
)
async def app_stats(
    request: Request,
    app_id: UUID,
    user: StatsUser,
    service: ServiceDep,
    days: DaysQuery = DEFAULT_DAYS,
) -> dict[str, Any]:
    return await service.series(app_id, days=days)
