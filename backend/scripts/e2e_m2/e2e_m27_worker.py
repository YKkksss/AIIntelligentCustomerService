"""M2.7 live check: real Celery worker + beat close idle conversations on time.

    # terminal 1/2 (same env as the API):
    celery -A aics.tasks.celery_app worker -Q maintenance --pool=threads -c 2 -l info
    celery -A aics.tasks.celery_app beat -l info
    PYTHONPATH=. uv run python -m scripts.e2e_m2.e2e_m27_worker

Creates an app with idle_close_minutes=2 / idle_notice_before_minutes=1, starts a
conversation with one user message and polls the DB every second until it is closed.
Notice is due 60 s and close 120 s after the last activity; with a 30 s beat both must land
within +30 s (plus task latency). No model calls.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aics.core.config import get_settings
from aics.db.models import App, Message, User
from aics.modules.apps.service import AppService
from aics.modules.chat.repository import ConversationRepository, MessageRepository
from aics.modules.chat.schemas import NewConversation, NewMessage

OUT = Path(__file__).resolve().parent / "results" / "e2e_m27_worker.json"
TOLERANCE_S = 35  # 30 s beat + task latency


async def main() -> None:
    engine = create_async_engine(get_settings().db_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as s:
        u = (await s.execute(select(User).where(User.email == "e2e-m22@local"))).scalar()
        if u is None:
            u = User(email="e2e-m22@local", display_name="e2e", password_hash="x")  # noqa: S106 - eval-only user
            s.add(u)
            await s.flush()
        app = await AppService(s).create_app(name=f"e2e-m27-{int(time.time())}", created_by=u.id)
        await AppService(s).update_app(
            app.id,
            {
                "session_config": {"idle_close_minutes": 2, "idle_notice_before_minutes": 1},
                "handoff_config": {"handoff_queue_timeout_minutes": 1},
            },
        )
        conv = await ConversationRepository(s).create(app.id, NewConversation(channel="api"))
        await MessageRepository(s).append(app.id, conv.id, NewMessage(role="user", content="在吗"))
        await s.commit()
    async with sessions() as s:
        c = await ConversationRepository(s).get_or_404(app.id, conv.id)
        t0 = c.last_activity_at
    print("last_activity_at", t0.isoformat())
    events: dict[str, float] = {}
    deadline = time.monotonic() + 120 + 90
    while time.monotonic() < deadline:
        async with sessions() as s:
            rows = (
                await s.execute(
                    select(Message.kind, Message.created_at).where(
                        Message.conversation_id == conv.id, Message.role == "system"
                    )
                )
            ).all()
            c = await ConversationRepository(s).get_or_404(app.id, conv.id)
        for kind, at in rows:
            if kind not in events:
                events[kind] = (at - t0).total_seconds()
                print(kind, f"+{events[kind]:.1f}s")
        if c.status == "closed":
            break
        await asyncio.sleep(1)
    async with sessions() as s:
        a = await s.get(App, app.id)
        a.status = "deleting"  # keep later sweeps / runs from seeing it as a live app
        await s.commit()
    await engine.dispose()
    notice, close = events.get("idle_notice"), events.get("idle_closed")
    report = {
        "notice_after_s": notice,
        "close_after_s": close,
        "notice_ok": notice is not None and 60 <= notice <= 60 + TOLERANCE_S,
        "close_ok": close is not None and 120 <= close <= 120 + TOLERANCE_S,
        "status": c.status,
        "close_reason": c.close_reason,
    }
    OUT.write_text(json.dumps(report, indent=1))  # noqa: ASYNC240
    print(json.dumps(report))


if __name__ == "__main__":
    asyncio.run(main())
