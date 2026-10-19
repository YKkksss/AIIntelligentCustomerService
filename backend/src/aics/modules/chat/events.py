"""会话事件 outbox（ADR-0016）：与状态变更**同事务**写事件行，顺手排 Webhook 投递。

事件流（SSE）与出站 Webhook 是同一份数据：`conversation_events` 是事实来源，
`webhook_deliveries` 只是「这个事件要不要发出去、发到哪一步了」的记录。

`emit` 只负责落库；提交之后调 `publish_pending` 把本事务记下的 `seq` 逐条广播到
`aics:conv:{id}`（订阅方回表读全文）。发布失败只记日志：事件已落库，SSE 重连按
`Last-Event-ID` 补发，Webhook 由重试扫描兜底。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import structlog
from redis.exceptions import RedisError
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from aics.core.errors import ConversationNotFoundError
from aics.db.base import new_id
from aics.db.models.conversation import Conversation
from aics.db.models.event import EVENT_TYPES, ConversationEvent, WebhookDelivery
from aics.modules.chat.repository import ConversationRepository
from aics.modules.webhooks.payloads import build_payload

log = structlog.get_logger()

#: 只有这两个渠道的事件会出站：`debug` 写事件（后台排查）但不投递，`eval` 一律不写。
DELIVERED_CHANNELS = ("api", "openai")
#: `eval` 渠道不产生会话事件（审核流水线用自己的表，§3.15）。
SKIPPED_CHANNELS = ("eval",)
#: `message.created` 覆盖的范围（§7.3）：转人工排队/接入期间的每条消息，以及任何 system 消息。
MESSAGE_EVENT_STATUSES = ("pending", "accepted")
#: `session.info` 里累积「已落库、待广播」的 `(conversation_id, seq)`，提交后清空。
PENDING_KEY = "aics.events.pending"


def channel(conversation_id: uuid.UUID, *, prefix: str = "aics:") -> str:
    """会话事件流的 pub/sub 频道（部署 §5：`aics:conv:{id}`，ACL 频道模式 `&aics:*`）。"""
    return f"{prefix}conv:{conversation_id}"


class Publisher(Protocol):
    """提交之后的唤醒通知：只带 `seq`，订阅方回表读全文（ADR-0016）。"""

    async def publish(self, conversation_id: uuid.UUID, seq: int) -> None: ...


class RedisPublisher:
    """Redis pub/sub 实现；发布失败只记日志（事件已落库，由重连补发与重试扫描兜底）。"""

    def __init__(self, redis: Any, *, prefix: str = "aics:") -> None:
        self.redis = redis
        self.prefix = prefix

    async def publish(self, conversation_id: uuid.UUID, seq: int) -> None:
        try:
            await self.redis.publish(channel(conversation_id, prefix=self.prefix), str(seq))
        except RedisError as e:  # 事件已落库：SSE 重连补发 + Webhook 重试扫描兜底
            log.warning("chat.event_publish_failed", error_type=type(e).__name__)


async def publish_pending(session: AsyncSession, publisher: Publisher | None) -> int:
    """提交**之后**调用：广播本事务 `emit()` 记下的 `seq`，返回广播的会话数。

    同一会话只广播一次（带本事务的最大 `seq`）：订阅方本来就按自己的游标回表读，
    多播几次只是噪音。调用方不传 `publisher`（本机脚本、单测、没配 Redis 的部署）时只清空待发队列。
    """
    pending: list[tuple[uuid.UUID, int]] = session.info.pop(PENDING_KEY, None) or []
    if publisher is None:
        return 0
    latest: dict[uuid.UUID, int] = {}
    for conversation_id, seq in pending:
        latest[conversation_id] = max(seq, latest.get(conversation_id, 0))
    for conversation_id, seq in latest.items():
        await publisher.publish(conversation_id, seq)
    return len(latest)


def needs_message_event(role: str, handoff_status: str) -> bool:
    """这条消息要不要单独发 `message.created`（§7.3）。

    普通 AI 问答不进事件流：接入方关心的是「人工介入期间发生了什么」与系统提示，
    其余内容它有自己的渠道（`GET …/messages`）。
    """
    return role == "system" or handoff_status in MESSAGE_EVENT_STATUSES


@dataclass(frozen=True)
class Target:
    """事件归属：会话、应用、渠道与接入方用户标识（payload 的 `user_id`）。"""

    conversation_id: uuid.UUID
    app_id: uuid.UUID
    channel: str
    user_id: str | None = None

    @classmethod
    def of(cls, conversation: Conversation, *, user_id: str | None = None) -> Target:
        return cls(
            conversation_id=conversation.id,
            app_id=conversation.app_id,
            channel=conversation.channel,
            # 接入方传入的用户标识（可能为空），不是 AICS 的内部 uuid
            user_id=conversation.external_user_id if user_id is None else user_id,
        )


@dataclass(frozen=True)
class WebhookTarget:
    """应用的 Webhook 目标：只看地址与「配没配密钥」，不解密、不碰明文。"""

    url: str | None = None
    has_secret: bool = False

    @property
    def ready(self) -> bool:
        return bool(self.url) and self.has_secret

    @classmethod
    def of(cls, webhook: Any | None) -> WebhookTarget:
        """从 `AppConfig.webhook`（或 `apps.webhook_config` 解析结果）取只读字段。"""
        if webhook is None:
            return cls()
        return cls(
            url=getattr(webhook, "url", None),
            has_secret=bool(getattr(webhook, "secret_enc", None)),
        )


async def emit(
    session: AsyncSession,
    target: Target,
    type: str,
    data: Mapping[str, Any] | None = None,
    *,
    webhook: WebhookTarget | None = None,
    occurred_at: datetime | None = None,
) -> ConversationEvent | None:
    """写一条会话事件，返回事件行；`eval` 渠道返回 `None`（调用方无需分支）。

    `seq` 由 `conversations.event_seq` 在同一事务内递增分配：同一会话的事件因此严格
    递增、无空洞，SSE 的 `id:` 行与 `Last-Event-ID` 补发都以它为准（ADR-0016）。
    Webhook 投递只在「渠道要出站 + 应用配了地址与密钥」时排一条 `pending`，
    `next_attempt_at` 直接等于当前时刻：立即投递失败时有重试扫描兜底（§3.16）。
    """
    if type not in EVENT_TYPES:
        raise ValueError(f"unknown conversation event type: {type}")
    if target.channel in SKIPPED_CHANNELS:
        return None
    at = occurred_at or datetime.now(UTC)
    seq = (
        await session.execute(
            update(Conversation)
            .where(
                Conversation.id == target.conversation_id,
                Conversation.app_id == target.app_id,
            )
            .values(event_seq=Conversation.event_seq + 1)
            .returning(Conversation.event_seq)
            .execution_options(synchronize_session=False)
        )
    ).scalar_one_or_none()
    if seq is None:
        raise ConversationNotFoundError(details={"id": str(target.conversation_id)})
    event_id = new_id()
    event = ConversationEvent(
        id=event_id,
        app_id=target.app_id,
        conversation_id=target.conversation_id,
        seq=seq,
        type=type,
        data=dict(data or {}),
        created_at=at,
    )
    session.add(event)
    session.info.setdefault(PENDING_KEY, []).append((target.conversation_id, seq))
    if webhook is not None and webhook.ready and target.channel in DELIVERED_CHANNELS:
        session.add(
            WebhookDelivery(
                id=event_id,
                event_id=event_id,
                app_id=target.app_id,
                conversation_id=target.conversation_id,
                event=type,
                seq=seq,
                payload=build_payload(
                    delivery_id=event_id,
                    event=type,
                    app_id=target.app_id,
                    conversation_id=target.conversation_id,
                    user_id=target.user_id,
                    seq=seq,
                    occurred_at=at,
                    data=event.data,
                ),
                status="pending",
                attempts=0,
                next_attempt_at=at,
                created_at=at,
            )
        )
    return event


async def emit_all(
    session: AsyncSession,
    app_id: uuid.UUID,
    conversation_id: uuid.UUID,
    items: Sequence[tuple[str, Mapping[str, Any]]],
    *,
    webhook: WebhookTarget | None = None,
    conversation: Any | None = None,
) -> None:
    """按给定顺序发一组会话事件（同一事务）。

    后台扫描（`chat.lifecycle` / `handoff.sweep`）只拿到会话 id，会话行按 id 现读：
    `seq` 分配与 payload 的 `user_id`（接入方用户标识）都要它；读不到就静默跳过——
    状态变更已经落库，少一条事件不该让整轮扫描回滚。
    """
    conv = conversation
    if conv is None:
        conv = await ConversationRepository(session).get(app_id, conversation_id)
    if conv is None:
        return
    target = Target.of(conv)
    for type_, data in items:
        await emit(session, target, type_, data, webhook=webhook)
