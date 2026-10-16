"""M2.1 (integration): conversation / message repositories on real PG."""

from __future__ import annotations

from typing import Any

import pytest

from aics.core.errors import NotFoundError, ValidationAppError
from aics.core.tokens import count_tokens
from aics.db.models import Conversation
from aics.modules.apps.service import AppService
from aics.modules.apps.system_settings import SystemSettingsService
from aics.modules.chat.repository import (
    ConversationClosedError,
    ConversationRepository,
    MessageRepository,
)
from aics.modules.chat.schemas import NewConversation, NewMessage

pytestmark = pytest.mark.integration


async def test_append_computes_tokens_search_text_and_counters(pg: dict[str, Any]) -> None:
    app = pg["app_a"]
    async with pg["sessions"]() as s:
        convs, msgs = ConversationRepository(s), MessageRepository(s)
        c = await convs.create(app, NewConversation(channel="api", external_user_id="u1"))
        text = "我的蓝牙耳机X2订单 A123 想退货"
        u = await msgs.append(
            app, c.id, NewMessage(role="user", content=text), custom_dict=["蓝牙耳机X2"]
        )
        assert u.token_count == count_tokens(text) > 0
        assert "蓝牙耳机X2" in u.search_text.split() and "退货" in u.search_text.split()
        a = await msgs.append(
            app, c.id, NewMessage(role="assistant", content="可以退货[1]", answer_type="answer")
        )
        sysm = await msgs.append(
            app, c.id, NewMessage(role="system", content="即将结束", kind="idle_notice")
        )
        assert sysm.search_text == "" and sysm.token_count > 0
        await s.commit()

        await s.refresh(c)
        assert c.message_count == 3
        assert c.title == text[:50]
        # system message does not count as activity
        assert c.last_activity_at == a.created_at
        assert c.last_message_at == sysm.created_at
        ordered = await msgs.list_for_conversation(app, c.id)
        assert [m.id for m in ordered] == [u.id, a.id, sysm.id]
        assert [m.id for m in await msgs.list_for_conversation(app, c.id, before=sysm.id)] == [
            u.id,
            a.id,
        ]


async def test_activity_resets_idle_notice(pg: dict[str, Any]) -> None:
    app = pg["app_a"]
    async with pg["sessions"]() as s:
        convs, msgs = ConversationRepository(s), MessageRepository(s)
        c = await convs.create(app, NewConversation(channel="api"))
        await convs.update(app, c.id, idle_notice_sent_at=c.created_at)
        await msgs.append(app, c.id, NewMessage(role="user", content="还在吗"))
        await s.commit()
        await s.refresh(c)
        assert c.idle_notice_sent_at is None


async def test_isolation_between_apps(pg: dict[str, Any]) -> None:
    a, b = pg["app_a"], pg["app_b"]
    async with pg["sessions"]() as s:
        convs, msgs = ConversationRepository(s), MessageRepository(s)
        c = await convs.create(a, NewConversation(channel="api"))
        m = await msgs.append(a, c.id, NewMessage(role="user", content="hi"))
        await s.commit()
        assert await convs.get(b, c.id) is None
        assert await msgs.get(b, m.id) is None
        assert await msgs.list_for_conversation(b, c.id) == []
        assert await convs.list_recent(b) == []
        assert [x.id for x in await convs.list_recent(a)] == [c.id]
        with pytest.raises(NotFoundError):
            await msgs.append(b, c.id, NewMessage(role="user", content="hack"))
        with pytest.raises(NotFoundError):
            await convs.create(b, NewConversation(channel="api", previous_conversation_id=c.id))
        assert await convs.close(b, c.id, reason="idle") is False


async def test_closed_conversation(pg: dict[str, Any]) -> None:
    app = pg["app_a"]
    async with pg["sessions"]() as s:
        convs, msgs = ConversationRepository(s), MessageRepository(s)
        c = await convs.create(app, NewConversation(channel="api"))
        assert await convs.close(app, c.id, reason="idle") is True
        assert await convs.close(app, c.id, reason="idle") is False  # idempotent
        with pytest.raises(ConversationClosedError):
            await msgs.append(app, c.id, NewMessage(role="user", content="hi"))
        await msgs.append(
            app, c.id, NewMessage(role="system", content="已结束", kind="idle_closed")
        )
        nxt = await convs.create(app, NewConversation(channel="api", previous_conversation_id=c.id))
        await s.commit()
        got = await s.get(Conversation, c.id)
        assert got is not None and got.status == "closed" and got.closed_at is not None
        assert nxt.previous_conversation_id == c.id


async def test_messages_are_immutable(pg: dict[str, Any]) -> None:
    app = pg["app_a"]
    async with pg["sessions"]() as s:
        convs, msgs = ConversationRepository(s), MessageRepository(s)
        c = await convs.create(app, NewConversation(channel="api"))
        m = await msgs.append(app, c.id, NewMessage(role="user", content="hi"))
        with pytest.raises(ValueError):
            await msgs.update(app, m.id, content="edited")
        assert await msgs.update(app, m.id, retrieval_trace=None) == 1


async def test_system_settings_default_session(pg: dict[str, Any]) -> None:
    async with pg["sessions"]() as s:
        svc = SystemSettingsService(s)
        assert (await svc.get_default_session_config()).idle_close_minutes == 10
        with pytest.raises(ValidationAppError):
            await svc.set_default_session_config({"idle_close_minutes": 0}, updated_by=None)
        cfg = await svc.set_default_session_config(
            {
                "idle_close_minutes": 20,
                "idle_notice_before_minutes": 2,
                "idle_close_notice": "再见",
            },
            updated_by=pg["user_id"],
        )
        assert cfg.idle_close_minutes == 20
        assert (await svc.get_default_session_config()).idle_close_minutes == 20
        app_svc = await AppService.load(s)
        assert app_svc.platform_session.idle_close_minutes == 20
        await s.rollback()  # keep the seeded default for other tests
