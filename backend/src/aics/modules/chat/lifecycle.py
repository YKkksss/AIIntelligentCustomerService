"""Idle notice and idle close of conversations (会话生命周期与转人工 §2, §3.4; ADR-0010).

One mechanism only: beat runs `aics.idle_sweep` every 30 s, which calls `sweep()`.

Per app, with its effective `session_config` (app values over `system_settings` defaults):

1. notice — `idle_notice_before_minutes > 0`, no notice yet, idle for at least
   `close − before` minutes but *not yet* `close` minutes → `system/idle_notice` message with
   `idle_close_notice`, `idle_notice_sent_at = now`;
2. close — idle for `idle_close_minutes` → `closed` / `close_reason='idle'` / `closed_at`, plus an
   empty `system/idle_closed` timeline marker.

Rules:
- every write is a conditional single-row update (`status='active' AND last_activity_at = the
  value read`), and the system message is written in the same transaction, so a new message in
  between wins, and repeated / concurrent sweeps never write a notice or close twice;
- skipped: `channel='eval'`; paused conversations — `accepted`, and `pending` when the app's
  `handoff_ai_mode='wait'` (§3.4); apps that are `deleting` (they close with `app_deleted`);
- a conversation already past its close time when first seen (sweeper was down) is closed
  without a back-dated notice (design discussion 0008);
- an app whose stored config no longer validates falls back per object to the platform
  defaults (logged), so one broken app never stops the sweep for the others;
- each step handles at most `batch` conversations per app per run; the rest wait 30 s.
Events (`conversation.idle_notice` / `conversation.closed`) go out in M3.5 through `notify`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
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
from aics.modules.apps.service import AppService
from aics.modules.chat.repository import ConversationRepository, MessageRepository
from aics.modules.chat.schemas import NewMessage
from aics.rag.config import EffectiveSessionConfig, HandoffConfig, SessionConfig

log = structlog.get_logger()

SWEEP_BATCH = 500  # per app per step per run (参数默认值与依据)
# Smallest possible notice / close delay (idle_close_minutes ≥ 1): pre-filter for apps.
MIN_IDLE = timedelta(minutes=1)

Notify = Callable[[str, UUID, UUID], Awaitable[None]]  # (event, app_id, conversation_id)


@dataclass(frozen=True)
class LifecyclePolicy:
    session: EffectiveSessionConfig
    pause_pending: bool  # handoff_ai_mode == "wait"


@dataclass
class SweepResult:
    apps: int = 0
    noticed: int = 0
    closed: int = 0
    skipped_apps: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "apps": self.apps,
            "noticed": self.noticed,
            "closed": self.closed,
            "skipped_apps": len(self.skipped_apps),
        }


def policy_of(app: App, platform: EffectiveSessionConfig) -> LifecyclePolicy:
    """Effective idle settings of one app, tolerant of an invalid stored config."""
    try:
        session = SessionConfig.model_validate(dict(app.session_config or {})).resolve(platform)
    except ValidationError:
        log.warning("chat.lifecycle_invalid_session_config", app_id=str(app.id))
        session = platform
    try:
        handoff = HandoffConfig.model_validate(dict(app.handoff_config or {}))
    except ValidationError:
        log.warning("chat.lifecycle_invalid_handoff_config", app_id=str(app.id))
        handoff = HandoffConfig()
    return LifecyclePolicy(session=session, pause_pending=handoff.handoff_ai_mode == "wait")


async def _db_now(s: AsyncSession) -> datetime:
    return (await s.execute(select(func.now()))).scalar_one()


async def sweep(
    sessions: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
    batch: int = SWEEP_BATCH,
    notify: Notify | None = None,
) -> SweepResult:
    """One idle sweep over all apps. `now` is injectable for tests (default: DB clock)."""
    res = SweepResult()
    async with sessions() as s:
        now = now or await _db_now(s)
        platform = (await AppService.load(s)).platform_session
        app_ids = await ConversationRepository(s).idle_app_ids_all_apps(before=now - MIN_IDLE)
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
        policies = {a.id: (policy_of(a, platform), list(a.custom_dict or [])) for a in apps}
        res.skipped_apps = [str(a) for a in app_ids if a not in policies]
    for app_id, (policy, custom_dict) in policies.items():
        res.apps += 1
        try:
            n, c = await _sweep_app(sessions, app_id, policy, custom_dict, now, batch, notify)
        except Exception:  # one app failing must not stop the others; next run retries
            log.exception("chat.lifecycle_app_failed", app_id=str(app_id))
            continue
        res.noticed += n
        res.closed += c
    if res.noticed or res.closed:
        log.info("chat.idle_sweep", **res.as_dict())
    return res


async def _sweep_app(
    sessions: async_sessionmaker[AsyncSession],
    app_id: UUID,
    policy: LifecyclePolicy,
    custom_dict: list[str],
    now: datetime,
    batch: int,
    notify: Notify | None,
) -> tuple[int, int]:
    cfg = policy.session
    close_at = now - timedelta(minutes=cfg.idle_close_minutes)
    noticed = closed = 0
    if cfg.idle_notice_before_minutes > 0:
        notice_at = now - timedelta(minutes=cfg.idle_close_minutes - cfg.idle_notice_before_minutes)
        async with sessions() as s:
            rows = await ConversationRepository(s).idle_candidates(
                app_id,
                before=notice_at,
                after=close_at,
                unnoticed=True,
                pause_pending=policy.pause_pending,
                limit=batch,
            )
        for cid, seen in rows:
            async with sessions() as s:
                ok = await ConversationRepository(s).mark_idle_notice(
                    app_id, cid, seen_activity=seen, at=now, pause_pending=policy.pause_pending
                )
                if not ok:
                    continue  # a new message (or a pause) arrived in between
                await MessageRepository(s).append(
                    app_id,
                    cid,
                    NewMessage(role="system", kind="idle_notice", content=cfg.idle_close_notice),
                    custom_dict=custom_dict,
                )
                await s.commit()
            noticed += 1
            metrics.inc("aics_idle_notice_total", app_id=str(app_id))
            if notify is not None:
                await notify("conversation.idle_notice", app_id, cid)
    async with sessions() as s:
        rows = await ConversationRepository(s).idle_candidates(
            app_id, before=close_at, pause_pending=policy.pause_pending, limit=batch
        )
    for cid, seen in rows:
        async with sessions() as s:
            ok = await ConversationRepository(s).close_idle(
                app_id, cid, seen_activity=seen, at=now, pause_pending=policy.pause_pending
            )
            if not ok:
                continue
            await MessageRepository(s).append(
                app_id,
                cid,
                NewMessage(role="system", kind="idle_closed", content=""),
                custom_dict=custom_dict,
            )
            await s.commit()
        closed += 1
        metrics.inc("aics_idle_closed_total", app_id=str(app_id))
        if notify is not None:
            await notify("conversation.closed", app_id, cid)
    return noticed, closed


IDLE_SWEEP_TASK = "aics.idle_sweep"
IDLE_SWEEP_INTERVAL_S = 30.0  # beat period (§2.2: notice / close accurate to ≤ 30 s)


async def run_idle_sweep_job(settings: Any = None) -> dict[str, Any]:
    """Body of the Celery task / `aics-cli sweep-idle`: a NullPool engine per run, because
    each task runs in its own event loop (asyncio.run)."""
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
