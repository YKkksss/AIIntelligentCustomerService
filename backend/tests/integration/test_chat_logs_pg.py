"""M4.5 ③ (integration): 会话日志、强制释放、待回访与反馈运营面 over real PG.

What only a real database can prove:
- 筛选（渠道默认口径、意图、点踩、关键字）真的走 SQL，关键字命中靠 `messages.tsv`（jieba 词元）；
- 游标翻页在同一 `last_activity_at` 的多条会话上不重不漏；
- 强制释放写 `handoff.released` 事件 + `audit_logs` 且第二次 409；
- 「标记已回访」写清理人与备注并留审计；
- 反馈 `handled` 落库、跨应用 404。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select, update
from tests.chat_support import make_runtime

from aics.core.errors import ForbiddenError, UnauthenticatedError
from aics.core.security import AuthedUser
from aics.db.models import App, AuditLog, Conversation, ConversationEvent, Feedback
from aics.main import create_app
from aics.modules.audit.sink import PgAuditSink
from aics.modules.chat.repository import ConversationRepository, MessageRepository
from aics.modules.chat.schemas import NewConversation, NewMessage
from aics.modules.feedback.runtime import FeedbackRuntime

pytestmark = pytest.mark.integration

HEADERS = {"authorization": "Bearer admin"}
USER_ID = uuid.uuid4()


class FakeAuthorizer:
    def __init__(
        self, *, actor_id: UUID = USER_ID, deny: str | None = None, super_admin: bool = True
    ) -> None:
        self.deny = deny
        self.actor = AuthedUser(
            id=actor_id,
            email="admin@example.com",
            display_name="管理员",
            is_super_admin=super_admin,
        )

    async def authenticate(self, authorization: str | None) -> AuthedUser:
        if authorization != "Bearer admin":
            raise UnauthenticatedError(details={"reason": "missing_token"})
        return self.actor

    async def authorize(self, user: Any, *, app_id: Any, permission: str) -> None:
        if self.deny is not None and permission == self.deny:
            raise ForbiddenError(details={"reason": "missing_permission", "permission": permission})


class AuditRuntime:
    def sink(self) -> PgAuditSink:
        return PgAuditSink()


@pytest.fixture
async def ops(env: dict[str, Any]) -> Any:
    app = create_app()
    app.state.chat_runtime = make_runtime(env["sessions"])
    app.state.feedback_runtime = FeedbackRuntime(sessions=env["sessions"])
    app.state.audit_runtime = AuditRuntime()
    async with env["sessions"]() as s:
        owner = await s.scalar(select(App.created_by).where(App.id == env["app_id"]))
    assert owner is not None
    authorizer = FakeAuthorizer(actor_id=owner)
    app.state.authorizer = authorizer
    runtime = app.state.chat_runtime
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t", timeout=30) as client:
        yield {
            "client": client,
            "app_id": env["app_id"],
            "env": env,
            "runtime": runtime,
            "user_id": owner,
        }
    await runtime.drain()


async def new_conversation(
    env: dict[str, Any], *, channel: str = "api", at: datetime | None = None
) -> Conversation:
    async with env["sessions"]() as s:
        conv = await ConversationRepository(s).create(
            env["app_id"],
            NewConversation(channel=channel, external_user_id=f"u-{uuid.uuid4().hex[:6]}"),
        )
        if at is not None:
            await s.execute(
                update(Conversation)
                .where(Conversation.id == conv.id)
                .values(created_at=at, last_activity_at=at)
            )
        await s.commit()
        return conv


async def add_turn(
    env: dict[str, Any],
    conversation_id: UUID,
    *,
    question: str,
    answer: str,
    intent: str | None = None,
) -> tuple[UUID, UUID]:
    async with env["sessions"]() as s:
        repo = MessageRepository(s)
        user_msg = await repo.append(
            env["app_id"],
            conversation_id,
            NewMessage(role="user", content=question),
        )
        bot = await repo.append(
            env["app_id"],
            conversation_id,
            NewMessage(
                role="assistant",
                content=answer,
                answer_type="answer",
                intent=intent,  # type: ignore[arg-type]
                intent_confidence=0.9 if intent else None,
                intent_source="rule" if intent else None,
            ),
        )
        await s.commit()
        return user_msg.id, bot.id


async def test_default_channels_hide_debug_and_eval(ops: dict[str, Any]) -> None:
    env = ops["env"]
    api = await new_conversation(env, channel="api")
    openai = await new_conversation(env, channel="openai")
    debug = await new_conversation(env, channel="debug")

    response = await ops["client"].get(
        f"/api/v1/admin/apps/{ops['app_id']}/conversations", headers=HEADERS
    )
    assert response.status_code == 200
    ids = {row["id"] for row in response.json()["items"]}
    assert {str(api.id), str(openai.id)} <= ids
    assert str(debug.id) not in ids

    response = await ops["client"].get(
        f"/api/v1/admin/apps/{ops['app_id']}/conversations",
        headers=HEADERS,
        params={"channel": "debug"},
    )
    assert [row["id"] for row in response.json()["items"]] == [str(debug.id)]


async def test_keyword_search_hits_the_tsv_index(ops: dict[str, Any]) -> None:
    env = ops["env"]
    refund = await new_conversation(env)
    invoice = await new_conversation(env)
    await add_turn(env, refund.id, question="收到货后还能退款吗", answer="支持七天无理由退款")
    await add_turn(env, invoice.id, question="怎么开发票", answer="在订单页申请")

    response = await ops["client"].get(
        f"/api/v1/admin/apps/{ops['app_id']}/conversations", headers=HEADERS, params={"q": "退款"}
    )
    assert [row["id"] for row in response.json()["items"]] == [str(refund.id)]


async def test_intent_and_downvote_filters(ops: dict[str, Any]) -> None:
    env = ops["env"]
    faq = await new_conversation(env)
    _, bot = await add_turn(env, faq.id, question="怎么退款", answer="在订单页操作", intent="kb_qa")
    other = await new_conversation(env)
    await add_turn(env, other.id, question="你好", answer="你好", intent="chitchat")
    async with env["sessions"]() as s:
        s.add(Feedback(app_id=env["app_id"], message_id=bot, rating=-1, comment="没用"))
        await s.commit()

    base = f"/api/v1/admin/apps/{ops['app_id']}/conversations"
    by_intent = await ops["client"].get(base, headers=HEADERS, params={"intent": "kb_qa"})
    assert [row["id"] for row in by_intent.json()["items"]] == [str(faq.id)]
    assert by_intent.json()["items"][0]["intent"] == "kb_qa"

    downvoted = await ops["client"].get(base, headers=HEADERS, params={"downvoted": "true"})
    assert [row["id"] for row in downvoted.json()["items"]] == [str(faq.id)]
    assert downvoted.json()["items"][0]["downvotes"] == 1


async def test_cursor_pagination_is_stable_on_equal_timestamps(ops: dict[str, Any]) -> None:
    env = ops["env"]
    at = datetime.now(UTC).replace(microsecond=0)
    rows = [await new_conversation(env, at=at - timedelta(seconds=i)) for i in range(3)]
    base = f"/api/v1/admin/apps/{ops['app_id']}/conversations"

    first = await ops["client"].get(base, headers=HEADERS, params={"limit": 2})
    body = first.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"]

    second = await ops["client"].get(
        base, headers=HEADERS, params={"limit": 2, "cursor": body["next_cursor"]}
    )
    seen = [row["id"] for row in body["items"]] + [row["id"] for row in second.json()["items"]]
    assert len(seen) == len(set(seen)) == 3
    assert {str(r.id) for r in rows} == set(seen)


async def test_detail_returns_messages_summary_and_facts(ops: dict[str, Any]) -> None:
    env = ops["env"]
    conv = await new_conversation(env)
    await add_turn(
        env,
        conv.id,
        question="我的手机号 13812345678，还能退货吗",
        answer="可以，七天无理由",
        intent="kb_qa",
    )
    async with env["sessions"]() as s:
        await s.execute(
            update(Conversation)
            .where(Conversation.id == conv.id)
            .values(summary={"topics": ["退货"]}, summary_version=2)
        )
        await s.commit()

    response = await ops["client"].get(
        f"/api/v1/admin/apps/{ops['app_id']}/conversations/{conv.id}", headers=HEADERS
    )
    assert response.status_code == 200
    body = response.json()
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    # 超级管理员看原文
    assert "13812345678" in body["messages"][0]["content"]
    assert body["summary"]["version"] == 2
    assert body["conversation"]["message_count"] >= 2


async def test_detail_masks_for_a_viewer(env: dict[str, Any]) -> None:
    conv = await new_conversation(env)
    await add_turn(env, conv.id, question="手机号 13812345678", answer="收到", intent="kb_qa")
    app = create_app()
    app.state.chat_runtime = make_runtime(env["sessions"])
    app.state.authorizer = FakeAuthorizer(deny="app.write", super_admin=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t", timeout=30
    ) as client:
        response = await client.get(
            f"/api/v1/admin/apps/{env['app_id']}/conversations/{conv.id}", headers=HEADERS
        )
    assert response.status_code == 200
    assert "13812345678" not in response.json()["messages"][0]["content"]
    await app.state.chat_runtime.drain()


async def test_force_release_emits_event_audits_and_conflicts(ops: dict[str, Any]) -> None:
    env = ops["env"]
    conv = await new_conversation(env)
    async with env["sessions"]() as s:
        await s.execute(
            update(Conversation)
            .where(Conversation.id == conv.id)
            .values(handoff_status="pending", handoff_requested_at=datetime.now(UTC))
        )
        await s.commit()

    url = f"/api/v1/admin/apps/{ops['app_id']}/conversations/{conv.id}/release"
    response = await ops["client"].post(url, headers=HEADERS, json={"note": "坐席下班"})
    assert response.status_code == 200, response.text
    row = response.json()
    assert row["handoff_status"] == "released"
    assert row["handoff_end_reason"] == "admin"

    async with env["sessions"]() as s:
        events = list(
            (
                await s.scalars(
                    select(ConversationEvent).where(
                        ConversationEvent.conversation_id == conv.id,
                        ConversationEvent.type == "handoff.released",
                    )
                )
            ).all()
        )
        assert len(events) == 1
        assert events[0].data["released_by"] == "admin"
        assert events[0].data["reason"] == "admin"
        audits = list(
            (
                await s.scalars(
                    select(AuditLog).where(
                        AuditLog.app_id == env["app_id"],
                        AuditLog.action == "chat.conversation.release",
                    )
                )
            ).all()
        )
        assert len(audits) == 1
        assert audits[0].target_id == conv.id

    again = await ops["client"].post(url, headers=HEADERS, json={})
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "HANDOFF_STATE_CONFLICT"


async def test_clear_callback_marks_and_audits(ops: dict[str, Any]) -> None:
    env = ops["env"]
    conv = await new_conversation(env)
    async with env["sessions"]() as s:
        await s.execute(
            update(Conversation)
            .where(Conversation.id == conv.id)
            .values(
                needs_callback=True,
                callback_reason="queue_timeout",
                callback_requested_at=datetime.now(UTC),
                handoff_status="released",
            )
        )
        await s.commit()

    listed = await ops["client"].get(
        f"/api/v1/admin/apps/{ops['app_id']}/callbacks", headers=HEADERS
    )
    assert [row["id"] for row in listed.json()["items"]] == [str(conv.id)]

    response = await ops["client"].post(
        f"/api/v1/admin/apps/{ops['app_id']}/callbacks/{conv.id}",
        headers=HEADERS,
        json={"note": "已电话回复"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["needs_callback"] is False
    assert response.json()["callback_note"] == "已电话回复"

    async with env["sessions"]() as s:
        row = await s.get(Conversation, conv.id)
        assert row is not None
        assert row.callback_cleared_at is not None
        assert row.callback_cleared_by == ops["user_id"]
        audits = list(
            (
                await s.scalars(
                    select(AuditLog).where(
                        AuditLog.app_id == env["app_id"], AuditLog.action == "chat.callback.clear"
                    )
                )
            ).all()
        )
        assert len(audits) == 1

    again = await ops["client"].post(
        f"/api/v1/admin/apps/{ops['app_id']}/callbacks/{conv.id}", headers=HEADERS, json={}
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "CALLBACK_STATE_CONFLICT"


async def test_feedback_list_and_handle(ops: dict[str, Any]) -> None:
    env = ops["env"]
    conv = await new_conversation(env)
    _, bot = await add_turn(
        env, conv.id, question="怎么退款", answer="在订单页操作", intent="kb_qa"
    )
    await add_turn(env, conv.id, question="还有什么", answer="没有", intent="kb_qa")
    async with env["sessions"]() as s:
        feedback = Feedback(
            app_id=env["app_id"], message_id=bot, rating=-1, comment="答非所问", source="api"
        )
        s.add(feedback)
        await s.commit()
        feedback_id = feedback.id

    base = f"/api/v1/admin/apps/{ops['app_id']}/feedback"
    listed = await ops["client"].get(base, headers=HEADERS, params={"rating": -1})
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == str(feedback_id)
    assert items[0]["message"]["intent"] == "kb_qa"
    assert items[0]["conversation_id"] == str(conv.id)

    handled = await ops["client"].patch(
        f"{base}/{feedback_id}", headers=HEADERS, json={"handled": True}
    )
    assert handled.status_code == 200, handled.text
    assert handled.json()["handled"] is True

    async with env["sessions"]() as s:
        audits = list(
            (
                await s.scalars(
                    select(AuditLog).where(
                        AuditLog.app_id == env["app_id"], AuditLog.action == "feedback.handle"
                    )
                )
            ).all()
        )
        assert len(audits) == 1
        assert audits[0].target_type == "feedback"


async def test_feedback_of_another_app_is_404(ops: dict[str, Any], pg: dict[str, Any]) -> None:
    env = ops["env"]
    conv = await new_conversation(env)
    _, bot = await add_turn(env, conv.id, question="问", answer="答", intent="kb_qa")
    async with env["sessions"]() as s:
        feedback = Feedback(app_id=env["app_id"], message_id=bot, rating=1)
        s.add(feedback)
        await s.commit()
        feedback_id = feedback.id

    # 用另一个应用（`pg["app_b"]`，由夹具负责清理）的路径访问同一条反馈 → 404（作用域过滤）
    response = await ops["client"].patch(
        f"/api/v1/admin/apps/{pg['app_b']}/feedback/{feedback_id}", headers=HEADERS, json={}
    )
    assert response.status_code == 404
    assert response.json()["error"]["details"]["feedback_id"] == str(feedback_id)
