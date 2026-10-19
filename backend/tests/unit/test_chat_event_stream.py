"""`chat.event_stream` 与 `chat.events` 广播侧（M3.5 ②）的单元测试。

真 PG + Redis 的行为（补发游标、实时推送、断开重连）由集成用例覆盖；这里钉住的是
纯逻辑：`Last-Event-ID` 解析、帧格式与 Webhook payload 一致、待广播队列的去重与清空、
发布失败的吞掉，以及 `stream()` 的「先订阅 → 补发 → 心跳 → 关闭即结束」骨架。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from aics.core.errors import ValidationAppError
from aics.db.models.event import ConversationEvent
from aics.modules.chat import event_stream, events

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
APP = uuid.uuid4()
CONV = uuid.uuid4()


# --------------------------------------------------------------------------- 解析与帧


def test_last_event_id_defaults_to_zero() -> None:
    assert event_stream.parse_last_event_id(None) == 0
    assert event_stream.parse_last_event_id("") == 0
    assert event_stream.parse_last_event_id("   ") == 0


def test_last_event_id_accepts_digits_with_spaces() -> None:
    assert event_stream.parse_last_event_id("7") == 7
    assert event_stream.parse_last_event_id(" 12 ") == 12
    assert event_stream.parse_last_event_id("0") == 0


@pytest.mark.parametrize("raw", ["abc", "-1", "1.5", "7a"])
def test_invalid_last_event_id_is_a_validation_error(raw: str) -> None:
    with pytest.raises(ValidationAppError) as err:
        event_stream.parse_last_event_id(raw)
    assert err.value.status == 422
    assert err.value.details["errors"][0]["loc"] == ["header", "Last-Event-ID"]


def event(seq: int = 3, type_: str = "message.created", data: Any = None) -> ConversationEvent:
    return ConversationEvent(
        id=uuid.uuid4(),
        app_id=APP,
        conversation_id=CONV,
        seq=seq,
        type=type_,
        data={"message": {"id": "m1"}} if data is None else data,
        created_at=NOW,
    )


def test_frame_carries_id_and_the_webhook_payload() -> None:
    row = event(seq=9)
    raw = event_stream.frame(row, user_id="u_10086")
    assert raw.startswith("id: 9\ndata: {") and raw.endswith("\n\n")
    # 不写 `event:` 行：事件名在 payload 里，浏览器默认的 onmessage 也能收到
    assert "\nevent: " not in raw
    body = json.loads(raw.split("data: ", 1)[1])
    assert body["id"] == str(row.id)
    assert body["event"] == "message.created"
    assert body["app_id"] == str(APP)
    assert body["conversation_id"] == str(CONV)
    assert body["user_id"] == "u_10086"
    assert body["seq"] == 9
    assert body["occurred_at"] == NOW.isoformat()
    assert body["data"] == {"message": {"id": "m1"}}


# --------------------------------------------------------------------------- 广播


def test_channel_uses_the_redis_prefix() -> None:
    assert events.channel(CONV) == f"aics:conv:{CONV}"
    assert events.channel(CONV, prefix="t:") == f"t:conv:{CONV}"


class FakeRedis:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    async def publish(self, channel: str, payload: str) -> None:
        if self.fail:
            raise RedisConnectionError("boom")
        self.calls.append((channel, payload))


async def test_redis_publisher_sends_the_seq_on_the_conversation_channel() -> None:
    redis = FakeRedis()
    await events.RedisPublisher(redis, prefix="aics:").publish(CONV, 4)
    assert redis.calls == [(f"aics:conv:{CONV}", "4")]


async def test_redis_publisher_swallows_connection_errors() -> None:
    # 事件已经落库：发布失败只记日志，由 SSE 重连补发与 Webhook 重试扫描兜底
    await events.RedisPublisher(FakeRedis(fail=True)).publish(CONV, 4)


def session_with_info(*pending: tuple[uuid.UUID, int]) -> Any:
    s = SimpleNamespace()
    s.info = {events.PENDING_KEY: list(pending)}
    return s


async def test_publish_pending_dedupes_per_conversation_and_clears() -> None:
    other = uuid.uuid4()
    s = session_with_info((CONV, 1), (CONV, 3), (other, 2))

    class Recorder:
        def __init__(self) -> None:
            self.seen: list[tuple[uuid.UUID, int]] = []

        async def publish(self, conversation_id: uuid.UUID, seq: int) -> None:
            self.seen.append((conversation_id, seq))

    rec = Recorder()
    assert await events.publish_pending(s, rec) == 2
    assert rec.seen == [(CONV, 3), (other, 2)]  # 同一会话只播一次，带本事务最大 seq
    assert events.PENDING_KEY not in s.info  # 取走即清空


async def test_publish_pending_without_publisher_only_clears() -> None:
    s = session_with_info((CONV, 1))
    assert await events.publish_pending(s, None) == 0
    assert events.PENDING_KEY not in s.info


async def test_emit_remembers_the_seq_for_the_after_commit_publish() -> None:
    s = FakeEmitSession(seq=5)
    s.info = {}
    await events.emit(
        s, events.Target(conversation_id=CONV, app_id=APP, channel="api"), "message.created"
    )
    assert s.info[events.PENDING_KEY] == [(CONV, 5)]


class FakeEmitSession:
    def __init__(self, seq: int) -> None:
        self.seq = seq
        self.info: dict[str, Any] = {}

    async def execute(self, stmt: Any) -> Any:
        return SimpleNamespace(scalar_one_or_none=lambda: self.seq)

    def add(self, obj: Any) -> None:
        pass


# --------------------------------------------------------------------------- stream()


class FakePubSub:
    def __init__(self, messages: list[Any] | None = None) -> None:
        self.messages = list(messages or [])
        self.subscribed: list[str] = []
        self.heartbeats: list[float] = []
        self.unsubscribed = False
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        self.subscribed.append(channel)

    # 形状要与 redis-py 的 pubsub 一致：event_stream 传的是 timeout=
    async def get_message(
        self,
        *,
        ignore_subscribe_messages: bool,
        timeout: float,  # noqa: ASYNC109
    ) -> Any:
        assert ignore_subscribe_messages is True
        self.heartbeats.append(timeout)
        return self.messages.pop(0) if self.messages else None

    async def unsubscribe(self) -> None:
        self.unsubscribed = True

    async def aclose(self) -> None:
        self.closed = True


class FakeRedisStream:
    def __init__(self, messages: list[Any] | None = None) -> None:
        self.pubsub_obj = FakePubSub(messages)

    def pubsub(self) -> FakePubSub:
        return self.pubsub_obj


class FakeSessionFactory:
    """`async with sessions() as s` + `ConversationEventRepository(s).list_after(...)`。"""

    def __init__(self, batches: list[list[ConversationEvent]], status: str = "active") -> None:
        self.batches = list(batches)
        self.status = status
        self.after_seqs: list[int] = []

    def __call__(self) -> FakeSessionFactory:
        return self

    async def __aenter__(self) -> Any:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


async def collect(app_id: uuid.UUID = APP, **kw: Any) -> list[str]:
    """消费到流结束；加超时，免得某条用例的假 pub/sub 永远没消息把整套单测挂住。"""

    async def run() -> list[str]:
        return [chunk async for chunk in event_stream.stream(app_id=app_id, **kw)]

    return await asyncio.wait_for(run(), timeout=5)


def install_stream(
    monkeypatch: pytest.MonkeyPatch, factory: FakeSessionFactory
) -> FakeSessionFactory:
    class _Events:
        def __init__(self, session: Any) -> None:
            self.session = session

        async def list_after(
            self, app_id: uuid.UUID, conversation_id: uuid.UUID, *, after_seq: int, limit: int
        ) -> list[ConversationEvent]:
            factory.after_seqs.append(after_seq)
            return factory.batches.pop(0) if factory.batches else []

    class _Conversations:
        def __init__(self, session: Any) -> None:
            pass

        async def get_or_404(self, app_id: uuid.UUID, conversation_id: uuid.UUID) -> Any:
            return SimpleNamespace(external_user_id="u_1", status=factory.status)

    monkeypatch.setattr(event_stream, "ConversationEventRepository", _Events)
    monkeypatch.setattr(event_stream, "ConversationRepository", _Conversations)
    return factory


async def test_stream_replays_backlog_then_ends_on_closed_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = install_stream(
        monkeypatch,
        FakeSessionFactory(
            [[event(seq=1), event(seq=2, type_="conversation.closed", data={"reason": "idle"})]]
        ),
    )
    chunks = await collect(sessions=factory, redis=None, prefix="aics:", conversation_id=CONV)
    assert [c.split("\n", 1)[0] for c in chunks] == ["id: 1", "id: 2"]
    # 没有 Redis 时补发完就结束（本机脚本、未接 Redis 的部署），不会挂住
    assert factory.after_seqs == [0]


async def test_stream_subscribes_before_replaying(monkeypatch: pytest.MonkeyPatch) -> None:
    # 会话已结束：补发完这条就断开，不会停在心跳循环里
    factory = install_stream(monkeypatch, FakeSessionFactory([[event(seq=4)]], status="closed"))
    redis = FakeRedisStream()
    chunks = await collect(
        sessions=factory, redis=redis, prefix="t:", conversation_id=CONV, last_seq=3
    )
    assert redis.pubsub_obj.subscribed == [f"t:conv:{CONV}"]  # 先订阅
    assert factory.after_seqs[0] == 3  # 再按 Last-Event-ID 补发
    assert chunks and chunks[0].startswith("id: 4")
    assert redis.pubsub_obj.unsubscribed and redis.pubsub_obj.closed


async def test_stream_pings_while_idle_and_pushes_live_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = install_stream(
        monkeypatch,
        FakeSessionFactory(
            [
                [],  # 先把补发放完
                [event(seq=5, type_="message.created")],  # 频道通知后回表读到的新事件
                [event(seq=6, type_="conversation.closed", data={"reason": "idle"})],
            ]
        ),
    )
    redis = FakeRedisStream(messages=[None, {"type": "message", "data": b"5"}])
    chunks = await collect(
        sessions=factory, redis=redis, prefix="aics:", conversation_id=CONV, heartbeat_s=15.0
    )
    assert chunks[0] == ": ping\n\n"  # 补发为空 + 频道没消息 → 心跳
    assert [c.split("\n", 1)[0] for c in chunks[1:]] == ["id: 5", "id: 6"]
    assert redis.pubsub_obj.heartbeats == [15.0]


async def test_stream_ends_immediately_when_the_conversation_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = install_stream(monkeypatch, FakeSessionFactory([], status="closed"))
    redis = FakeRedisStream()
    assert await collect(sessions=factory, redis=redis, prefix="aics:", conversation_id=CONV) == []
    assert redis.pubsub_obj.unsubscribed and redis.pubsub_obj.closed


async def test_stream_swallows_pubsub_close_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    factory = install_stream(monkeypatch, FakeSessionFactory([[]], status="closed"))

    class BadPubSub(FakePubSub):
        async def unsubscribe(self) -> None:
            raise RedisConnectionError("gone")

        async def aclose(self) -> None:
            raise RedisConnectionError("gone")

    class BadRedis:
        def pubsub(self) -> FakePubSub:
            return BadPubSub()

    assert (
        await collect(sessions=factory, redis=BadRedis(), prefix="aics:", conversation_id=CONV)
        == []
    )
