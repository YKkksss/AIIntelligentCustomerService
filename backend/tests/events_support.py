"""会话事件 outbox 的单元测试替身（M3.5 ①）。

`events.emit` / `events.emit_all` 在 service、sweep、lifecycle 里到处都是调用点，
单测只关心「谁在什么时候发了什么事件」，真正的 SQL 语义由 `test_chat_events.py`
（假 session）与集成用例（真 PG）覆盖。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import pytest

from aics.modules.chat import events


@dataclass
class RecordedEvent:
    type: str
    data: dict[str, Any]
    target: Any = None
    app_id: UUID | None = None
    conversation_id: UUID | None = None
    webhook: Any = None


@dataclass
class EventRecorder:
    """`events.emit` / `events.emit_all` 的记录器，行为与真实现一致（返回 None 表示不落库）。"""

    events: list[RecordedEvent] = field(default_factory=list)

    def types(self) -> list[str]:
        return [e.type for e in self.events]

    def of(self, type_: str) -> list[RecordedEvent]:
        return [e for e in self.events if e.type == type_]

    async def emit(
        self,
        session: Any,
        target: Any,
        type: str,
        data: dict[str, Any] | None = None,
        *,
        webhook: Any = None,
        occurred_at: Any = None,
    ) -> None:
        self.events.append(
            RecordedEvent(type=type, data=dict(data or {}), target=target, webhook=webhook)
        )
        return None

    async def emit_all(
        self,
        session: Any,
        app_id: UUID,
        conversation_id: UUID,
        items: list[tuple[str, dict[str, Any]]],
        *,
        webhook: Any = None,
        conversation: Any = None,
    ) -> None:
        for type_, data in items:
            self.events.append(
                RecordedEvent(
                    type=type_,
                    data=dict(data),
                    app_id=app_id,
                    conversation_id=conversation_id,
                    webhook=webhook,
                )
            )


def install(monkeypatch: pytest.MonkeyPatch) -> EventRecorder:
    """把 `chat.events` 的两个写入口换成记录器（所有调用方共用同一模块对象）。"""
    recorder = EventRecorder()
    monkeypatch.setattr(events, "emit", recorder.emit)
    monkeypatch.setattr(events, "emit_all", recorder.emit_all)
    return recorder
