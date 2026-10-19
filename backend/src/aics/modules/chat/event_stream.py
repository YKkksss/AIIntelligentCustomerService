"""会话事件流（SSE）：先订阅频道，再按 `Last-Event-ID` 补发（ADR-0016，开放接口参考 §1）。

帧里只有 `id:`（= 会话事件序号 `seq`）与 `data:`（= 与出站 Webhook 完全相同的 payload），
**不写 `event:` 行**——事件名在 payload 的 `event` 字段里，这样浏览器默认的 `onmessage`
也能收到。`: ping` 每 15 秒一次；推完 `conversation.closed` 就结束连接，连接时会话已经关闭的
补发完也结束。

顺序（ADR-0016 §4）：先 `SUBSCRIBE`，再查 `seq > Last-Event-ID` 补发，之后按频道通知回表读；
同一 `seq` 只发一次——订阅与补发之间的重叠由游标本身去掉。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aics.core.errors import ValidationAppError
from aics.db.models.event import ConversationEvent
from aics.modules.chat import events
from aics.modules.chat.event_repository import ConversationEventRepository
from aics.modules.chat.repository import ConversationRepository
from aics.modules.chat.sse import HEARTBEAT_S
from aics.modules.webhooks.payloads import build_payload

log = structlog.get_logger()

#: 推完这条事件就结束连接（§4：会话已经结束，不会再有事件）。
CLOSED_EVENT = "conversation.closed"
#: 每次回表补发的上限；读满就继续下一轮，直到读空。
READ_BATCH = 200


def parse_last_event_id(raw: str | None) -> int:
    """`Last-Event-ID` 请求头 → 起始游标；缺省 0（从头补发），不合法报 422。"""
    if raw is None or not raw.strip():
        return 0
    try:
        value = int(raw.strip())
    except ValueError as e:
        raise ValidationAppError(
            details={"errors": [{"loc": ["header", "Last-Event-ID"], "msg": "无效的事件序号"}]}
        ) from e
    if value < 0:
        raise ValidationAppError(
            details={"errors": [{"loc": ["header", "Last-Event-ID"], "msg": "无效的事件序号"}]}
        )
    return value


def frame(event: ConversationEvent, *, user_id: str | None) -> str:
    """一条 SSE 帧。`data` 与出站 Webhook 的 payload 逐字段相同（同一个构造器）。"""
    payload = build_payload(
        delivery_id=event.id,
        event=event.type,
        app_id=event.app_id,
        conversation_id=event.conversation_id,
        user_id=user_id,
        seq=event.seq,
        occurred_at=event.created_at,
        data=event.data,
    )
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    return f"id: {event.seq}\ndata: {body}\n\n"


async def _read(
    sessions: async_sessionmaker[AsyncSession], app_id: UUID, conversation_id: UUID, after_seq: int
) -> list[ConversationEvent]:
    """一次短事务读一批事件（连接期间不持有事务）。"""
    async with sessions() as s:
        return await ConversationEventRepository(s).list_after(
            app_id, conversation_id, after_seq=after_seq, limit=READ_BATCH
        )


async def _close(pubsub: Any) -> None:
    try:
        await pubsub.unsubscribe()
    except Exception as e:  # 连接已经断了也要继续 aclose
        log.warning("chat.event_unsubscribe_failed", error_type=type(e).__name__)
    try:
        await pubsub.aclose()
    except Exception as e:
        log.warning("chat.event_pubsub_close_failed", error_type=type(e).__name__)


async def stream(
    *,
    sessions: async_sessionmaker[AsyncSession],
    redis: Any,
    prefix: str,
    app_id: UUID,
    conversation_id: UUID,
    last_seq: int = 0,
    heartbeat_s: float = HEARTBEAT_S,
) -> AsyncIterator[str]:
    """事件流主体；`redis` 为 None 时退化为「补发一次就结束」（本机脚本、未接 Redis）。"""
    async with sessions() as s:
        conv = await ConversationRepository(s).get_or_404(app_id, conversation_id)
        user_id = conv.external_user_id
        closed_at_start = conv.status != "active"
    pubsub = None
    if redis is not None:
        pubsub = redis.pubsub()
        await pubsub.subscribe(events.channel(conversation_id, prefix=prefix))
    cursor = last_seq
    try:
        while True:
            rows = await _read(sessions, app_id, conversation_id, cursor)
            for event in rows:
                yield frame(event, user_id=user_id)
                cursor = event.seq
                if event.type == CLOSED_EVENT:
                    return
            if rows:
                continue  # 可能还有下一批
            if closed_at_start or pubsub is None:
                return  # 会话已结束：不会再有事件
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=heartbeat_s)
            if message is None:
                yield ": ping\n\n"
    finally:
        if pubsub is not None:
            await _close(pubsub)
