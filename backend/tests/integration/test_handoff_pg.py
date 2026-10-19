"""M3.4 ④ 集成：两种超时扫描、并发抢单与配置校验在真实 PG 上的行为（转人工 §3.6 / §3.8 / §3.10）。

时钟一律注入（`sweep(now=…)`、条件更新比对 `seen_*`），不 sleep。扫描是「读一批 → 逐行条件单行
更新」，把「读之后状态被别人改掉」这条竞态分支显式跑出来比赌调度更可靠；真并发抢单另有一条用例
（两个事务同时 `accept`，只有一个拿到 1 行）。断言落在 `conversations` 的 handoff / callback 列与
`messages` 里的系统消息上。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select, update
from tests.chat_support import make_runtime

from aics.core.config import get_settings
from aics.core.errors import ValidationAppError
from aics.db.models import App, Conversation, Message
from aics.main import create_app
from aics.modules.apikeys.service import ApiKeyAuthenticator, ApiKeyService
from aics.modules.apps.service import AppService
from aics.modules.chat.repository import ConversationRepository, MessageRepository
from aics.modules.chat.schemas import NewConversation, NewMessage
from aics.modules.chat.service import append_agent_message
from aics.modules.handoff import sweep as sweep_mod
from aics.modules.handoff.repository import HandoffRepository
from aics.rag.config import HandoffConfig

pytestmark = pytest.mark.integration
M = timedelta(minutes=1)

#: 一句就命中转人工关键词（`handoff.keywords` 默认含「人工」）的问法
ASK_HUMAN = "我要人工"


async def new_key(pg: dict[str, Any], redis: Any, app_id: UUID, scopes: list[str]) -> str:
    pepper = get_settings().api_key_pepper.get_secret_value()
    async with pg["sessions"]() as s:
        _, plain = await ApiKeyService(s, pepper=pepper, redis=redis).create(
            app_id, name="handoff-it", scopes=scopes, rpm_limit=1000
        )
        await s.commit()
    return plain


@pytest.fixture
async def api(pg: dict[str, Any], redis_it: Any) -> AsyncIterator[dict[str, Any]]:
    """真 HTTP（ASGI 直连）+ 假模型：只给「非服务时间走留言」这条路径用。"""
    app = create_app()
    app.state.authenticator = ApiKeyAuthenticator(
        pg["sessions"], pepper=get_settings().api_key_pepper.get_secret_value(), redis=redis_it
    )
    app.state.chat_runtime = make_runtime(pg["sessions"])
    key = await new_key(pg, redis_it, pg["app_a"], ["chat"])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t", timeout=30) as c:
        yield {"client": c, "key": key, **pg}
    await app.state.chat_runtime.drain()


async def new_conv(pg: dict[str, Any], app_id: UUID, **conv: Any) -> UUID:
    """一个带用户消息的会话；handoff / callback 列由调用方按场景直接摆好。"""
    async with pg["sessions"]() as s:
        c = await ConversationRepository(s).create(
            app_id, NewConversation(channel="api", external_user_id="u1")
        )
        await MessageRepository(s).append(app_id, c.id, NewMessage(role="user", content=ASK_HUMAN))
        if conv:
            await s.execute(update(Conversation).where(Conversation.id == c.id).values(**conv))
        await s.commit()
        return c.id


async def conv_of(pg: dict[str, Any], app_id: UUID, cid: UUID) -> Conversation:
    async with pg["sessions"]() as s:
        return await ConversationRepository(s).get_or_404(app_id, cid)


async def system_msgs(pg: dict[str, Any], cid: UUID) -> list[Message]:
    async with pg["sessions"]() as s:
        rows = await s.execute(
            select(Message)
            .where(Message.conversation_id == cid, Message.role == "system")
            .order_by(Message.created_at)
        )
        return list(rows.scalars())


async def handoff_cfg(pg: dict[str, Any], app_id: UUID) -> HandoffConfig:
    """应用级 `handoff_config` 的生效值（与 `sweep` 读的是同一份数据，但走独立解析）。"""
    async with pg["sessions"]() as s:
        app = await s.get(App, app_id)
        assert app is not None
        return HandoffConfig.model_validate(dict(app.handoff_config or {}))


async def patch_app(pg: dict[str, Any], app_id: UUID, patch: dict[str, Any]) -> None:
    async with pg["sessions"]() as s:
        await AppService(s).update_app(app_id, patch)
        await s.commit()


async def test_queue_timeout_releases_and_marks_callback(pg: dict[str, Any]) -> None:
    """§3.6：到点才释放，交还原因 `queue_timeout`，发话术、置待回访、空闲计时重置。"""
    a = pg["app_a"]
    await patch_app(pg, a, {"handoff_config": {"handoff_queue_timeout_minutes": 5}})
    cfg = await handoff_cfg(pg, a)
    cid = await new_conv(pg, a, handoff_status="pending", handoff_requested_at=func.now())
    requested = (await conv_of(pg, a, cid)).handoff_requested_at

    res = await sweep_mod.sweep(pg["sessions"], now=requested + 5 * M - timedelta(seconds=1))

    c = await conv_of(pg, a, cid)
    assert (c.handoff_status, c.needs_callback) == ("pending", False)
    assert res.queue_timeouts == 0 and await system_msgs(pg, cid) == []

    now = requested + 5 * M
    res = await sweep_mod.sweep(pg["sessions"], now=now)

    c = await conv_of(pg, a, cid)
    assert res.queue_timeouts == 1
    assert (c.status, c.handoff_status, c.handoff_end_reason) == (
        "active",
        "released",
        "queue_timeout",
    )
    assert c.handoff_released_at == now
    assert (c.needs_callback, c.callback_reason, c.callback_requested_at) == (
        True,
        "queue_timeout",
        now,
    )
    assert c.last_activity_at == now  # 会话回到 AI，按空闲规则重新计时
    msgs = await system_msgs(pg, cid)
    assert [(m.kind, m.content) for m in msgs] == [
        ("handoff_queue_timeout", cfg.queue_timeout_text)
    ]


async def test_queue_timeout_is_idempotent(pg: dict[str, Any]) -> None:
    """同一轮跑两次、以及很久以后再跑，都不会重复写话术或再改状态（§3.10）。"""
    a = pg["app_a"]
    cid = await new_conv(pg, a, handoff_status="pending", handoff_requested_at=func.now())
    now = (await conv_of(pg, a, cid)).handoff_requested_at + 6 * M

    assert (await sweep_mod.sweep(pg["sessions"], now=now)).queue_timeouts == 1
    assert (await sweep_mod.sweep(pg["sessions"], now=now)).queue_timeouts == 0
    later = await sweep_mod.sweep(pg["sessions"], now=now + 3 * 60 * M)

    c = await conv_of(pg, a, cid)
    assert later.queue_timeouts == 0 and c.handoff_status == "released"
    assert c.last_activity_at == now and c.callback_requested_at == now
    assert len(await system_msgs(pg, cid)) == 1


async def test_accepted_idle_closes_the_conversation(pg: dict[str, Any]) -> None:
    """§3.8：接入后双方无消息 → 释放 + 直接关会话 + 一个空的 `handoff_idle_closed` 标记。"""
    a = pg["app_a"]
    await patch_app(pg, a, {"handoff_config": {"handoff_accepted_idle_minutes": 30}})
    cid = await new_conv(
        pg, a, handoff_status="accepted", handoff_agent_id="ag_1", handoff_accepted_at=func.now()
    )
    silent_since = (await conv_of(pg, a, cid)).last_activity_at

    res = await sweep_mod.sweep(pg["sessions"], now=silent_since + 30 * M - timedelta(seconds=1))

    assert res.accepted_idle_closed == 0
    assert (await conv_of(pg, a, cid)).status == "active"

    now = silent_since + 30 * M
    res = await sweep_mod.sweep(pg["sessions"], now=now)

    c = await conv_of(pg, a, cid)
    assert res.accepted_idle_closed == 1
    assert (c.status, c.close_reason, c.closed_at) == ("closed", "handoff_idle", now)
    assert (c.handoff_status, c.handoff_end_reason) == ("released", "accepted_idle")
    assert c.needs_callback is False  # §3.8 不置待回访
    assert [(m.kind, m.content) for m in await system_msgs(pg, cid)] == [
        ("handoff_idle_closed", "")
    ]


async def test_queue_timeout_skips_conversation_accepted_in_between(pg: dict[str, Any]) -> None:
    """竞态分支：扫描读到 `pending` 之后坐席抢先接入 → 条件更新 0 行，一个字都不写。"""
    a = pg["app_a"]
    cid = await new_conv(pg, a, handoff_status="pending", handoff_requested_at=func.now())
    async with pg["sessions"]() as s:
        due = dict(await HandoffRepository(s).pending_due(a, before=datetime.now(UTC), limit=10))
    seen = due[cid]
    async with pg["sessions"]() as s:
        assert (
            await HandoffRepository(s).accept(a, cid, agent_id="ag_1", agent_name="小艾", at=seen)
            == 1
        )
        await s.commit()

    async with pg["sessions"]() as s:
        assert (
            await HandoffRepository(s).queue_timeout(
                a, cid, seen_requested_at=seen, at=seen + 6 * M
            )
            == 0
        )
        await s.commit()

    c = await conv_of(pg, a, cid)
    assert (c.handoff_status, c.handoff_agent_id) == ("accepted", "ag_1")
    assert c.handoff_end_reason is None and c.needs_callback is False
    assert await system_msgs(pg, cid) == []


async def test_accepted_idle_skips_conversation_that_just_spoke(pg: dict[str, Any]) -> None:
    """同上，另一条：坐席在本轮扫描之后说了话 → `last_activity_at` 变了，不关会话。"""
    a = pg["app_a"]
    cid = await new_conv(pg, a, handoff_status="accepted", handoff_agent_id="ag_1")
    seen = (await conv_of(pg, a, cid)).last_activity_at
    async with pg["sessions"]() as s:
        await append_agent_message(
            s, a, cid, role="human_agent", content="在的，请讲", agent_id="ag_1"
        )
        await s.commit()

    async with pg["sessions"]() as s:
        assert (
            await HandoffRepository(s).close_accepted_idle(
                a, cid, seen_activity=seen, at=seen + 31 * M
            )
            == 0
        )
        await s.commit()

    c = await conv_of(pg, a, cid)
    assert c.status == "active" and c.handoff_status == "accepted"
    assert await system_msgs(pg, cid) == []


async def test_concurrent_accept_only_one_wins(pg: dict[str, Any]) -> None:
    """两个事务同时抢单：条件单行更新让第二个拿到 0 行（不需要额外加锁）。"""
    a = pg["app_a"]
    cid = await new_conv(pg, a, handoff_status="pending", handoff_requested_at=func.now())

    async def grab(agent_id: str) -> int:
        async with pg["sessions"]() as s:
            n = await HandoffRepository(s).accept(
                a, cid, agent_id=agent_id, agent_name=None, at=datetime.now(UTC)
            )
            await s.commit()
            return n

    first, second = await asyncio.gather(grab("ag_1"), grab("ag_2"))

    assert sorted([first, second]) == [0, 1]
    c = await conv_of(pg, a, cid)
    assert c.handoff_status == "accepted"
    assert c.handoff_agent_id == ("ag_1" if first else "ag_2")


async def test_sweep_ignores_deleted_app(pg: dict[str, Any]) -> None:
    """软删除的应用不再被扫描（`deleted_at` 过滤）——否则已删除的租户还会被写消息。"""
    a = pg["app_a"]
    cid = await new_conv(pg, a, handoff_status="pending", handoff_requested_at=func.now())
    now = (await conv_of(pg, a, cid)).handoff_requested_at + 6 * M
    async with pg["sessions"]() as s:
        await s.execute(update(App).where(App.id == a).values(deleted_at=func.now()))
        await s.commit()

    res = await sweep_mod.sweep(pg["sessions"], now=now)

    assert res.queue_timeouts == 0
    c = await conv_of(pg, a, cid)
    assert c.handoff_status == "pending" and c.needs_callback is False
    assert await system_msgs(pg, cid) == []


async def test_offline_handoff_writes_message_and_callback(api: dict[str, Any]) -> None:
    """§3.7 走真接口：不在服务时间时不排队，回留言话术（`reply_by=system`）并置待回访。"""
    a = api["app_a"]
    await patch_app(
        api,
        a,
        {
            "handoff_config": {
                "service_hours": {
                    "timezone": "UTC",
                    "weekly": {d: [] for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")},
                }
            }
        },
    )
    cfg = await handoff_cfg(api, a)

    r = await api["client"].post(
        f"/api/v1/apps/{a}/chat",
        json={"question": ASK_HUMAN},
        headers={"Authorization": f"Bearer {api['key']}"},
    )

    assert r.status_code == 200, r.text
    blk = r.json()
    assert blk["handoff"] is True and blk["handoff_status"] == "released"
    assert blk["reply_by"] == "system" and blk.get("answer_type") is None
    assert blk["answer"] == cfg.offline_text and blk["handoff_reason"] == "keyword"

    cid = UUID(blk["conversation_id"])
    c = await conv_of(api, a, cid)
    assert (c.handoff_status, c.handoff_end_reason) == ("released", "offline")
    assert (c.needs_callback, c.callback_reason) == (True, "offline")
    assert c.handoff_agent_id is None and c.handoff_accepted_at is None
    assert [m.kind for m in await system_msgs(api, cid)] == ["handoff_offline"]


async def test_saving_queue_timeout_at_idle_close_is_rejected(pg: dict[str, Any]) -> None:
    """M2.7 校验的回归（§3.6）：`handoff_queue_timeout_minutes` 必须小于有效 `idle_close_minutes`。"""
    a = pg["app_a"]
    async with pg["sessions"]() as s:
        with pytest.raises(ValidationAppError) as e:
            await AppService(s).update_app(
                a, {"handoff_config": {"handoff_queue_timeout_minutes": 10}}
            )
    assert e.value.code == "VALIDATION_ERROR"
    assert e.value.details["errors"][0]["loc"] == [
        "handoff_config",
        "handoff_queue_timeout_minutes",
    ]

    await patch_app(pg, a, {"handoff_config": {"handoff_queue_timeout_minutes": 9}})  # 9 < 10 通过
