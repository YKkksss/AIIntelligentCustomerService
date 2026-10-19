"""`chat.events`：seq 分配、渠道过滤与投递排队的单元测试（M3.5 ①）。

真 SQL（`UPDATE … RETURNING`、事务、外键）由集成用例覆盖；这里用假 session 钉住
「什么情况下写什么行」这一层策略。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from aics.core.errors import ConversationNotFoundError
from aics.db.models.event import ConversationEvent, WebhookDelivery
from aics.modules.chat import events

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
CONV = uuid.uuid4()
APP = uuid.uuid4()


class _Result:
    def __init__(self, seq: int | None) -> None:
        self._seq = seq

    def scalar_one_or_none(self) -> int | None:
        return self._seq


class FakeSession:
    def __init__(self, seq: int | None = 7) -> None:
        self.seq = seq
        self.added: list[Any] = []
        self.statements: list[Any] = []
        self.info: dict[str, Any] = {}  # emit() 在这里记待广播的 seq（提交后 publish_pending 取走）

    async def execute(self, stmt: Any) -> _Result:
        self.statements.append(stmt)
        return _Result(self.seq)

    def add(self, obj: Any) -> None:
        self.added.append(obj)


def target(**kw: Any) -> events.Target:
    base: dict[str, Any] = {"conversation_id": CONV, "app_id": APP, "channel": "api"}
    return events.Target(**{**base, **kw})


def test_target_of_reads_the_conversation_row() -> None:
    conv = SimpleNamespace(id=CONV, app_id=APP, channel="openai", external_user_id="u_10086")
    assert events.Target.of(conv) == events.Target(
        conversation_id=CONV, app_id=APP, channel="openai", user_id="u_10086"
    )
    # 请求里带了接入方用户标识时以请求为准
    assert events.Target.of(conv, user_id="u_2").user_id == "u_2"


def test_webhook_target_requires_url_and_secret() -> None:
    assert events.WebhookTarget.of(None) == events.WebhookTarget()
    assert not events.WebhookTarget.of(SimpleNamespace(url="https://x", secret_enc=None)).ready
    assert not events.WebhookTarget.of(SimpleNamespace(url=None, secret_enc="enc")).ready
    assert events.WebhookTarget.of(SimpleNamespace(url="https://x", secret_enc="enc")).ready


def test_message_event_scope() -> None:
    assert events.needs_message_event("system", "none")
    assert events.needs_message_event("user", "pending")
    assert events.needs_message_event("human_agent", "accepted")
    assert not events.needs_message_event("assistant", "none")
    assert not events.needs_message_event("user", "released")


async def test_emit_writes_an_event_with_the_allocated_seq() -> None:
    s = FakeSession(seq=12)
    event = await events.emit(
        s, target(), "message.created", {"message": {"id": "m1"}}, occurred_at=NOW
    )
    assert isinstance(event, ConversationEvent)
    assert (event.seq, event.type, event.data["message"]["id"]) == (12, "message.created", "m1")
    assert event.created_at == NOW
    assert s.added == [event]
    # 没有 webhook（或没配密钥）就不排投递
    assert not any(isinstance(o, WebhookDelivery) for o in s.added)


async def test_emit_queues_a_delivery_for_api_channels() -> None:
    s = FakeSession(seq=3)
    webhook = events.WebhookTarget(url="https://receiver.example/aics", has_secret=True)
    event = await events.emit(
        s,
        target(user_id="u_10086"),
        "handoff.requested",
        {"reason": "intent_human"},
        webhook=webhook,
        occurred_at=NOW,
    )
    assert event is not None
    delivery = next(o for o in s.added if isinstance(o, WebhookDelivery))
    assert delivery.id == delivery.event_id == event.id  # 接入方按 id 去重（§3.16）
    assert (delivery.status, delivery.attempts, delivery.next_attempt_at) == ("pending", 0, NOW)
    payload = delivery.payload
    assert payload["id"] == str(event.id)
    assert payload["event"] == "handoff.requested"
    assert payload["app_id"] == str(APP)
    assert payload["conversation_id"] == str(CONV)
    assert payload["user_id"] == "u_10086"
    assert payload["seq"] == 3
    assert payload["occurred_at"] == NOW.isoformat()
    assert payload["data"] == {"reason": "intent_human"}


async def test_debug_channel_writes_the_event_without_delivering() -> None:
    s = FakeSession()
    webhook = events.WebhookTarget(url="https://receiver.example/aics", has_secret=True)
    event = await events.emit(
        s, target(channel="debug"), "conversation.closed", None, webhook=webhook
    )
    assert event is not None
    assert not any(isinstance(o, WebhookDelivery) for o in s.added)


async def test_eval_channel_writes_nothing() -> None:
    s = FakeSession()
    assert await events.emit(s, target(channel="eval"), "message.created") is None
    assert s.added == [] and s.statements == []


async def test_unknown_event_type_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown conversation event type"):
        await events.emit(FakeSession(), target(), "handoff.exploded")


async def test_missing_conversation_is_reported() -> None:
    with pytest.raises(ConversationNotFoundError):
        await events.emit(FakeSession(seq=None), target(), "message.created")


async def test_emit_all_uses_the_given_conversation() -> None:
    s = FakeSession(seq=1)
    conv = SimpleNamespace(id=CONV, app_id=APP, channel="api", external_user_id="u_1")
    await events.emit_all(
        s,
        APP,
        CONV,
        [
            ("message.created", {"message": {"id": "m"}}),
            ("conversation.closed", {"reason": "idle"}),
        ],
        conversation=conv,
    )
    assert [o.type for o in s.added if isinstance(o, ConversationEvent)] == [
        "message.created",
        "conversation.closed",
    ]


async def test_emit_all_skips_a_missing_conversation(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Repo:
        def __init__(self, session: Any) -> None:
            pass

        async def get(self, app_id: uuid.UUID, conversation_id: uuid.UUID) -> None:
            return None

    monkeypatch.setattr(events, "ConversationRepository", _Repo)
    s = FakeSession()
    await events.emit_all(s, APP, CONV, [("conversation.closed", {"reason": "idle"})])
    assert s.added == []
