"""Webhook payload 与事件的固定格式（第三方接入指南 §7.2–7.3）。

只做「拼装」：入参是标量与事件 `data`，不碰数据库、不加密、不签名——这样
`chat.events`（写 outbox）与 `webhooks.service`（投递）拿到的是同一份字节。
时间统一发 UTC（`+00:00`），接入方按 ISO 8601 解析。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol


class _Message(Protocol):
    """`messages` 行的只读视图（payload 需要的最小字段集）。"""

    id: uuid.UUID
    role: str
    kind: str
    content: str
    agent_id: str | None
    answer_type: str | None
    created_at: datetime


def iso(value: datetime) -> str:
    """ISO 8601（UTC）：`2026-10-15T07:30:00+00:00`。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def message_dict(message: _Message) -> dict[str, Any]:
    """`message.created` / `conversation.idle_notice` 里的 `message` 对象。"""
    return {
        "id": str(message.id),
        "role": message.role,
        "kind": message.kind,
        "content": message.content,
        "agent_id": message.agent_id,
        "answer_type": message.answer_type,
        "created_at": iso(message.created_at),
    }


def recent_messages(messages: Sequence[_Message]) -> list[dict[str, Any]]:
    """最近若干条消息（旧→新），坐席平台接单前看上下文用（§7.3 `handoff.requested`）。"""
    return [
        {
            "id": str(m.id),
            "role": m.role,
            "kind": m.kind,
            "content": m.content,
            "created_at": iso(m.created_at),
        }
        for m in messages
    ]


def summary_text(summary: Mapping[str, Any] | None) -> str | None:
    """滚动摘要（`conversations.summary`）的渲染文本；没有摘要时返回 `None`。

    §7.3：`summary` 取会话滚动摘要渲染文本，无摘要时为最近一轮改写问题——后者由调用方兜底。
    """
    if not summary:
        return None
    parts: list[str] = []
    goal = summary.get("user_goal")
    if goal:
        parts.append(str(goal))
    for key, label in (
        ("confirmed_info", "已确认"),
        ("solutions_given", "已给出"),
        ("open_issues", "待解决"),
    ):
        items = [str(v) for v in (summary.get(key) or []) if v]
        if items:
            parts.append(f"{label}：" + "；".join(items))
    return "；".join(parts) or None


def message_created(message: _Message) -> dict[str, Any]:
    """`message.created` 的 data（§7.3）。"""
    return {"message": message_dict(message)}


def handoff_requested(
    *,
    reason: str | None,
    intent: str | None,
    handoff_ai_mode: str,
    summary: str | None,
    facts: Mapping[str, Any] | None,
    recent: Sequence[_Message] = (),
) -> dict[str, Any]:
    """`handoff.requested` 的 data：坐席平台接单前要看的上下文（§7.3）。"""
    return {
        "reason": reason,
        "intent": intent,
        "handoff_ai_mode": handoff_ai_mode,
        "summary": summary,
        "facts": dict(facts or {}),
        "recent_messages": recent_messages(recent),
    }


def handoff_offline(
    *,
    message: _Message,
    reason: str | None,
    needs_callback: bool,
    summary: str | None,
    facts: Mapping[str, Any] | None,
    recent: Sequence[_Message] = (),
) -> dict[str, Any]:
    """`handoff.offline` 的 data：非服务时间的留言（§3.7 / §7.3）。"""
    return {
        "message": message_dict(message),
        "reason": reason,
        "needs_callback": needs_callback,
        "summary": summary,
        "facts": dict(facts or {}),
        "recent_messages": recent_messages(recent),
    }


def handoff_accepted(*, agent_id: str, agent_name: str | None) -> dict[str, Any]:
    """`handoff.accepted` 的 data。"""
    return {"agent_id": agent_id, "agent_name": agent_name}


def handoff_released(
    *, agent_id: str | None, note: str | None, released_by: str, reason: str
) -> dict[str, Any]:
    """`handoff.released` 的 data（`released_by` / `reason` 见 §7.3）。"""
    return {"agent_id": agent_id, "note": note, "released_by": released_by, "reason": reason}


def handoff_queue_timeout(
    *, message: _Message, waited_seconds: int, needs_callback: bool
) -> dict[str, Any]:
    """`handoff.queue_timeout` 的 data：坐席平台应撤下排队项（§3.6）。"""
    return {
        "message": message_dict(message),
        "waited_seconds": waited_seconds,
        "needs_callback": needs_callback,
    }


def idle_notice(*, message: _Message, close_at: datetime) -> dict[str, Any]:
    """`conversation.idle_notice` 的 data。"""
    return {"message": message_dict(message), "close_at": iso(close_at)}


def conversation_closed(*, reason: str) -> dict[str, Any]:
    """`conversation.closed` 的 data（`idle` / `handoff_idle`）。"""
    return {"reason": reason}


def build_payload(
    *,
    delivery_id: uuid.UUID,
    event: str,
    app_id: uuid.UUID,
    conversation_id: uuid.UUID | None,
    user_id: str | None,
    seq: int,
    occurred_at: datetime,
    data: Mapping[str, Any],
) -> dict[str, Any]:
    """公共字段 + `data`（§7.2）。`delivery_id` 同时是 `X-AICS-Delivery` 与 payload 的 `id`。"""
    return {
        "id": str(delivery_id),
        "event": event,
        "app_id": str(app_id),
        "conversation_id": str(conversation_id) if conversation_id is not None else None,
        "user_id": user_id,
        "seq": seq,
        "occurred_at": iso(occurred_at),
        "data": dict(data),
    }
