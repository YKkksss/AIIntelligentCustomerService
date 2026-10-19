"""转人工的两个超时（M3.4 ③，会话生命周期与转人工 §3.6 / §3.8 / §3.10）。

`idle_sweep` 每 30 秒按 §3.10 的顺序跑：**排队超时**（本模块）→ **接入后无消息超时**（本模块）
→ 空闲提醒与关闭（`chat.lifecycle`）。两步都靠条件单行更新，重复或并发执行都幂等。

- §3.6 排队超时：`pending` 且 `handoff_requested_at ≤ now − handoff_queue_timeout_minutes`
  → `released` / `handoff_end_reason='queue_timeout'` + `system/handoff_queue_timeout` 话术 +
  待回访（`needs_callback`）/ `last_activity_at = now`（会话回到 AI，按空闲规则重新计时）。
- §3.8 接入后无消息：`accepted` 且 `last_activity_at ≤ now − handoff_accepted_idle_minutes`
  → `released` / `handoff_end_reason='accepted_idle'` **并关会话**（`close_reason='handoff_idle'`）
  + 空的 `system/handoff_idle_closed` 时间线标记；不置待回访、不提前提醒。

逐应用推进，因为阈值与话术都在应用级 `handoff_config`（§3.5，无平台默认层）；某个应用配置读不出来
只跳过它自己，其它应用照跑。系统消息经 `chat.service.append_agent_message` 写，和坐席消息共用一份
`message_count` / `last_activity_at` 联动规则（§3.3）。

事件（`handoff.queue_timeout` / `handoff.released` / `conversation.closed`）在 M3.5 由
`events.emit(...)` 发出，这里只在调用点留 TODO。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aics.core import metrics
from aics.db.models import App
from aics.modules.chat.service import append_agent_message
from aics.modules.handoff.repository import HandoffRepository
from aics.rag.config import HandoffConfig

log = structlog.get_logger()

SWEEP_BATCH = 500  # 每个应用每轮每步的上限（参数默认值与依据）


@dataclass
class HandoffSweepResult:
    apps: int = 0
    skipped_apps: list[str] = field(default_factory=list)
    queue_timeouts: int = 0
    accepted_idle_closed: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "apps": self.apps,
            "queue_timeouts": self.queue_timeouts,
            "accepted_idle_closed": self.accepted_idle_closed,
            "skipped_apps": self.skipped_apps,
        }


def _config_of(app: App) -> HandoffConfig:
    """应用级 `handoff_config`；坏配置回落到默认值并记日志，不让一个应用拖垮整轮扫描。"""
    try:
        return HandoffConfig.model_validate(dict(app.handoff_config or {}))
    except ValidationError:
        log.warning("handoff.invalid_config", app_id=str(app.id))
        return HandoffConfig()


async def _db_now(s: AsyncSession) -> datetime:
    return (await s.execute(select(func.now()))).scalar_one()


async def sweep(
    sessions: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
    batch: int = SWEEP_BATCH,
) -> HandoffSweepResult:
    """One handoff sweep over all apps. `now` is injectable for tests (default: DB clock)."""
    res = HandoffSweepResult()
    async with sessions() as s:
        now = now or await _db_now(s)
        app_ids = await HandoffRepository(s).handoff_app_ids_all_apps()
        apps = (
            (
                await s.execute(
                    select(App).where(
                        App.id.in_(app_ids), App.deleted_at.is_(None), App.status != "deleting"
                    )
                )
            )
            .scalars()
            .all()
            if app_ids
            else []
        )
        configs = {a.id: _config_of(a) for a in apps}
        res.skipped_apps = [str(a) for a in app_ids if a not in configs]
    for app_id, cfg in configs.items():
        res.apps += 1
        try:
            timeouts, closed = await _sweep_app(sessions, app_id, cfg, now, batch)
        except Exception:  # one app failing must not stop the others; next run retries
            log.exception("handoff.sweep_app_failed", app_id=str(app_id))
            continue
        res.queue_timeouts += timeouts
        res.accepted_idle_closed += closed
    if res.queue_timeouts or res.accepted_idle_closed:
        log.info("handoff.sweep", **res.as_dict())
    return res


async def _sweep_app(
    sessions: async_sessionmaker[AsyncSession],
    app_id: UUID,
    cfg: HandoffConfig,
    now: datetime,
    batch: int,
) -> tuple[int, int]:
    """§3.10 的先后顺序：排队超时先于接入后无消息（两者互不影响，但顺序固定便于复现）。"""
    timeouts = await _sweep_queue_timeouts(sessions, app_id, cfg, now, batch)
    closed = await _sweep_accepted_idle(sessions, app_id, cfg, now, batch)
    return timeouts, closed


async def _sweep_queue_timeouts(
    sessions: async_sessionmaker[AsyncSession],
    app_id: UUID,
    cfg: HandoffConfig,
    now: datetime,
    batch: int,
) -> int:
    cutoff = now - timedelta(minutes=cfg.handoff_queue_timeout_minutes)
    async with sessions() as s:
        due = await HandoffRepository(s).pending_due(app_id, before=cutoff, limit=batch)
    done = 0
    for conversation_id, seen in due:
        async with sessions() as s:
            if not await HandoffRepository(s).queue_timeout(
                app_id, conversation_id, seen_requested_at=seen, at=now
            ):
                continue  # 期间被坐席接入、或又触发过一轮：留给下一次扫描
            await append_agent_message(
                s,
                app_id,
                conversation_id,
                role="system",
                kind="handoff_queue_timeout",
                content=cfg.queue_timeout_text,
            )
            await s.commit()
        done += 1
        metrics.inc("aics_handoff_queue_timeout_total", app_id=str(app_id))
        # TODO(M3.5): events.emit("handoff.queue_timeout") + message.created
    return done


async def _sweep_accepted_idle(
    sessions: async_sessionmaker[AsyncSession],
    app_id: UUID,
    cfg: HandoffConfig,
    now: datetime,
    batch: int,
) -> int:
    cutoff = now - timedelta(minutes=cfg.handoff_accepted_idle_minutes)
    async with sessions() as s:
        due = await HandoffRepository(s).accepted_due(app_id, before=cutoff, limit=batch)
    done = 0
    for conversation_id, seen in due:
        async with sessions() as s:
            if not await HandoffRepository(s).close_accepted_idle(
                app_id, conversation_id, seen_activity=seen, at=now
            ):
                continue  # 坐席或用户刚说过话（last_activity_at 变了）
            await append_agent_message(
                s,
                app_id,
                conversation_id,
                role="system",
                kind="handoff_idle_closed",
                content="",
            )
            await s.commit()
        done += 1
        metrics.inc("aics_handoff_accepted_idle_total", app_id=str(app_id))
        # TODO(M3.5): events.emit("handoff.released", released_by="system") + conversation.closed
    return done


async def run_handoff_sweep_job(settings: Any = None) -> dict[str, Any]:
    """Body of `idle_sweep` / `aics-cli sweep-idle`: a NullPool engine per run, because each
    task runs in its own event loop (asyncio.run)."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from aics.core.config import get_settings

    s = settings or get_settings()
    engine = create_async_engine(s.db_url, poolclass=NullPool)
    try:
        res = await sweep(async_sessionmaker(engine, expire_on_commit=False))
    finally:
        await engine.dispose()
    return res.as_dict()
