"""M2.7: idle notice / close sweep on real PG (会话生命周期与转人工 §2, §3.4).

`now` is injected relative to each conversation's own last_activity_at, so timing is exact
without sleeping. Assertions are per conversation (the sweep covers every app in the DB).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select, update
from tests.chat_support import make_runtime

from aics.db.models import Conversation, Message
from aics.modules.apps.service import AppService
from aics.modules.chat import lifecycle
from aics.modules.chat.repository import ConversationRepository, MessageRepository
from aics.modules.chat.schemas import NewConversation, NewMessage
from aics.modules.chat.service import ChatService, TurnRequest

pytestmark = pytest.mark.integration
M = timedelta(minutes=1)


async def new_conv(pg: dict[str, Any], app_id: UUID, channel: str = "api", **conv: Any) -> UUID:
    async with pg["sessions"]() as s:
        c = await ConversationRepository(s).create(
            app_id, NewConversation(channel=channel, external_user_id="u1")
        )
        await MessageRepository(s).append(app_id, c.id, NewMessage(role="user", content="退货"))
        if conv:
            await s.execute(update(Conversation).where(Conversation.id == c.id).values(**conv))
        await s.commit()
        return c.id


async def conv_of(pg: dict[str, Any], app_id: UUID, cid: UUID) -> Conversation:
    async with pg["sessions"]() as s:
        return await ConversationRepository(s).get_or_404(app_id, cid)


async def system_kinds(pg: dict[str, Any], cid: UUID) -> list[str]:
    async with pg["sessions"]() as s:
        rows = await s.execute(
            select(Message.kind)
            .where(Message.conversation_id == cid, Message.role == "system")
            .order_by(Message.created_at)
        )
        return list(rows.scalars())


async def at(pg: dict[str, Any], app_id: UUID, cid: UUID, delta: timedelta) -> datetime:
    return (await conv_of(pg, app_id, cid)).last_activity_at + delta


async def patch_app(pg: dict[str, Any], app_id: UUID, patch: dict[str, Any]) -> None:
    async with pg["sessions"]() as s:
        await AppService(s).update_app(app_id, patch)
        await s.commit()


async def test_notice_then_close_on_time(pg: dict[str, Any]) -> None:
    """Platform default: close after 10 min, notice 1 min before."""
    a = pg["app_a"]
    cid = await new_conv(pg, a)
    sw = pg["sessions"]
    await lifecycle.sweep(sw, now=await at(pg, a, cid, 9 * M - timedelta(seconds=1)))
    assert await system_kinds(pg, cid) == []
    await lifecycle.sweep(sw, now=await at(pg, a, cid, 9 * M))
    c = await conv_of(pg, a, cid)
    assert c.status == "active" and c.idle_notice_sent_at is not None
    async with sw() as s:
        msg = (
            await s.execute(
                select(Message).where(Message.conversation_id == cid, Message.role == "system")
            )
        ).scalar_one()
    assert msg.kind == "idle_notice" and msg.content == "如果没有其他问题，当前服务就结束咯~"
    # system messages do not reset the idle timer
    assert c.last_activity_at == (await conv_of(pg, a, cid)).last_activity_at
    await lifecycle.sweep(sw, now=await at(pg, a, cid, 10 * M - timedelta(seconds=1)))
    assert (await conv_of(pg, a, cid)).status == "active"
    now = await at(pg, a, cid, 10 * M)
    await lifecycle.sweep(sw, now=now)
    c = await conv_of(pg, a, cid)
    assert (c.status, c.close_reason, c.closed_at) == ("closed", "idle", now)
    assert await system_kinds(pg, cid) == ["idle_notice", "idle_closed"]


async def test_repeated_and_concurrent_sweeps_are_idempotent(pg: dict[str, Any]) -> None:
    a = pg["app_a"]
    cid = await new_conv(pg, a)
    sw = pg["sessions"]
    t = await at(pg, a, cid, 9 * M + timedelta(seconds=5))
    await asyncio.gather(*(lifecycle.sweep(sw, now=t) for _ in range(5)))
    await lifecycle.sweep(sw, now=t)
    assert await system_kinds(pg, cid) == ["idle_notice"]
    t = await at(pg, a, cid, 10 * M + timedelta(seconds=5))
    await asyncio.gather(*(lifecycle.sweep(sw, now=t) for _ in range(5)))
    await lifecycle.sweep(sw, now=t + M)
    assert await system_kinds(pg, cid) == ["idle_notice", "idle_closed"]


async def test_new_message_resets_timer_and_notice(pg: dict[str, Any]) -> None:
    a = pg["app_a"]
    cid = await new_conv(pg, a)
    sw = pg["sessions"]
    await lifecycle.sweep(sw, now=await at(pg, a, cid, 9 * M))
    assert (await conv_of(pg, a, cid)).idle_notice_sent_at is not None
    async with sw() as s:
        await MessageRepository(s).append(a, cid, NewMessage(role="user", content="还有问题"))
        await s.commit()
    c = await conv_of(pg, a, cid)
    assert c.idle_notice_sent_at is None
    # 10 min after the *first* activity is only ~1 min after the new one: nothing happens
    await lifecycle.sweep(sw, now=c.last_activity_at + 2 * M)
    assert (await conv_of(pg, a, cid)).status == "active"
    # a second notice is sent for the new idle period
    await lifecycle.sweep(sw, now=c.last_activity_at + 9 * M)
    assert await system_kinds(pg, cid) == ["idle_notice", "idle_notice"]


async def test_message_between_read_and_update_wins(pg: dict[str, Any]) -> None:
    """The conditional update fails when last_activity_at changed after the sweep read it."""
    a = pg["app_a"]
    cid = await new_conv(pg, a)
    seen = (await conv_of(pg, a, cid)).last_activity_at
    async with pg["sessions"]() as s:
        await MessageRepository(s).append(a, cid, NewMessage(role="user", content="在吗"))
        await s.commit()
    async with pg["sessions"]() as s:
        repo = ConversationRepository(s)
        assert not await repo.mark_idle_notice(a, cid, seen_activity=seen, at=seen + 9 * M)
        assert not await repo.close_idle(a, cid, seen_activity=seen, at=seen + 10 * M)
    assert (await conv_of(pg, a, cid)).status == "active"


async def test_late_sweep_closes_without_backdated_notice(pg: dict[str, Any]) -> None:
    a = pg["app_a"]
    cid = await new_conv(pg, a)
    await lifecycle.sweep(pg["sessions"], now=await at(pg, a, cid, 30 * M))
    assert await system_kinds(pg, cid) == ["idle_closed"]


async def test_app_session_config_and_no_notice(pg: dict[str, Any]) -> None:
    a, b = pg["app_a"], pg["app_b"]
    await patch_app(
        pg, a, {"session_config": {"idle_close_minutes": 30, "idle_notice_before_minutes": 0}}
    )
    await patch_app(
        pg,
        b,
        {"session_config": {"idle_close_minutes": 8, "idle_notice_before_minutes": 3,
                            "idle_close_notice": "B 应用的提醒"}},
    )  # fmt: skip
    ca, cb = await new_conv(pg, a), await new_conv(pg, b)
    sw = pg["sessions"]
    # sweeps are global, so check B (shorter timeouts) before moving the clock for A
    await lifecycle.sweep(sw, now=await at(pg, b, cb, 5 * M))
    async with sw() as s:
        msg = (
            await s.execute(
                select(Message).where(Message.conversation_id == cb, Message.role == "system")
            )
        ).scalar_one()
    assert msg.content == "B 应用的提醒"
    await lifecycle.sweep(sw, now=await at(pg, b, cb, 8 * M))
    assert (await conv_of(pg, b, cb)).status == "closed"
    await lifecycle.sweep(sw, now=await at(pg, a, ca, 29 * M))
    assert (await conv_of(pg, a, ca)).status == "active"  # platform 10 min no longer applies
    await lifecycle.sweep(sw, now=await at(pg, a, ca, 30 * M))
    assert (await conv_of(pg, a, ca)).status == "closed"
    assert await system_kinds(pg, ca) == ["idle_closed"]  # notice disabled


async def test_paused_and_eval_conversations_are_skipped(pg: dict[str, Any]) -> None:
    a, b = pg["app_a"], pg["app_b"]
    await patch_app(pg, b, {"handoff_config": {"handoff_ai_mode": "wait"}})
    accepted = await new_conv(pg, a, handoff_status="accepted")
    pending_continue = await new_conv(pg, a, handoff_status="pending")
    pending_wait = await new_conv(pg, b, handoff_status="pending")
    released_wait = await new_conv(pg, b, handoff_status="released")
    ev = await new_conv(pg, a, channel="eval")
    dbg = await new_conv(pg, a, channel="debug")
    later = (await conv_of(pg, a, dbg)).last_activity_at + 11 * M
    await lifecycle.sweep(pg["sessions"], now=later)
    status = {
        "accepted": (await conv_of(pg, a, accepted)).status,
        "pending_continue": (await conv_of(pg, a, pending_continue)).status,
        "pending_wait": (await conv_of(pg, b, pending_wait)).status,
        "released_wait": (await conv_of(pg, b, released_wait)).status,
        "eval": (await conv_of(pg, a, ev)).status,
        "debug": (await conv_of(pg, a, dbg)).status,
    }
    assert status == {
        "accepted": "active",
        "pending_continue": "closed",
        "pending_wait": "active",
        "released_wait": "closed",
        "eval": "active",
        "debug": "closed",
    }


async def test_invalid_stored_config_falls_back_to_platform(pg: dict[str, Any]) -> None:
    a = pg["app_a"]
    async with pg["sessions"]() as s:
        from aics.db.models import App

        await s.execute(
            update(App).where(App.id == a).values(session_config={"idle_close_minutes": 0})
        )
        await s.commit()
    cid = await new_conv(pg, a)
    await lifecycle.sweep(pg["sessions"], now=await at(pg, a, cid, 10 * M))
    assert (await conv_of(pg, a, cid)).status == "closed"


async def test_deleting_app_is_skipped(pg: dict[str, Any]) -> None:
    a = pg["app_a"]
    cid = await new_conv(pg, a)
    async with pg["sessions"]() as s:
        from aics.db.models import App

        await s.execute(update(App).where(App.id == a).values(status="deleting"))
        await s.commit()
    res = await lifecycle.sweep(pg["sessions"], now=await at(pg, a, cid, 20 * M))
    assert (await conv_of(pg, a, cid)).status == "active" and str(a) in res.skipped_apps


async def test_batch_limit_and_notify_hook(pg: dict[str, Any]) -> None:
    a = pg["app_a"]
    ids = [await new_conv(pg, a) for _ in range(3)]
    events: list[tuple[str, UUID]] = []

    async def notify(event: str, app_id: UUID, cid: UUID) -> None:
        if app_id == a:
            events.append((event, cid))

    t = await at(pg, a, ids[-1], 20 * M)
    await lifecycle.sweep(pg["sessions"], now=t, batch=2, notify=notify)
    assert sum(1 for e, c in events if c in ids) == 2
    await lifecycle.sweep(pg["sessions"], now=t, batch=2, notify=notify)
    assert sorted(c for e, c in events if c in ids) == sorted(ids)
    assert {e for e, _ in events} == {"conversation.closed"}


async def test_closed_by_sweep_then_chat_starts_new_conversation(pg: dict[str, Any]) -> None:
    a = pg["app_a"]
    svc = ChatService(make_runtime(pg["sessions"]))
    first = await svc.handle_turn(TurnRequest(app_id=a, question="退货要几天？"))
    cid = first.conversation_id
    await lifecycle.sweep(pg["sessions"], now=await at(pg, a, cid, 10 * M))
    res = await svc.handle_turn(
        TurnRequest(app_id=a, question="退款多久到账？", conversation_id=cid)
    )
    assert res.previous_conversation_id == cid and res.conversation_id != cid


async def test_sweep_waits_for_turn_holding_row_lock(pg: dict[str, Any]) -> None:
    """chat.service locks the row while preparing; the sweep's update then sees the new
    last_activity_at and does nothing (no 409 / no close of a just-used conversation)."""
    a = pg["app_a"]
    cid = await new_conv(pg, a)
    seen = (await conv_of(pg, a, cid)).last_activity_at
    async with pg["sessions"]() as chat_s:
        await ConversationRepository(chat_s).get_for_update(a, cid)

        async def sweeper() -> bool:
            async with pg["sessions"]() as s:
                ok = await ConversationRepository(s).close_idle(
                    a, cid, seen_activity=seen, at=seen + 10 * M
                )
                await s.commit()
                return ok

        task = asyncio.create_task(sweeper())
        await asyncio.sleep(0.3)
        assert not task.done()  # blocked by the row lock
        await MessageRepository(chat_s).append(a, cid, NewMessage(role="user", content="在的"))
        await chat_s.commit()
    assert await task is False
    assert (await conv_of(pg, a, cid)).status == "active"


async def test_run_idle_sweep_job_uses_its_own_engine(pg: dict[str, Any], pg_migrated: str) -> None:
    """Task body: real DB clock; a conversation idle for 11 minutes gets closed."""
    from types import SimpleNamespace

    a = pg["app_a"]
    cid = await new_conv(pg, a)
    async with pg["sessions"]() as s:
        await s.execute(
            update(Conversation)
            .where(Conversation.id == cid)
            .values(last_activity_at=Conversation.last_activity_at - 11 * M)
        )
        await s.commit()
    out = await lifecycle.run_idle_sweep_job(SimpleNamespace(db_url=pg_migrated))
    assert out["closed"] >= 1 and (await conv_of(pg, a, cid)).status == "closed"
