"""Adapter: aics.memory.MemoryService → rag.ports.MemoryPort.

rag and memory may not import each other (lint-imports), so the business layer converts the
request / result types. chat.service (M2.6) and the eval runner build RagPorts with it.
"""

from __future__ import annotations

from aics.memory.context import ContextQuery, HistoryMessage
from aics.memory.service import MemoryService
from aics.rag.ports import ChatTurn, ContextBundle, ContextRequest


class MemoryPortAdapter:
    def __init__(self, service: MemoryService) -> None:
        self.service = service

    async def load_context(self, req: ContextRequest) -> ContextBundle:
        loaded = await self.service.load_context(
            ContextQuery(
                app_id=req.app_id,
                conversation_id=req.conversation_id,
                question=req.question,
                budget=req.budget,
                keep_recent_turns=req.keep_recent_turns,
                enable_facts=req.enable_facts,
                inline_history=[HistoryMessage(t.role, t.content) for t in req.inline_history]
                if req.inline_history
                else None,
                temp_summary=req.temp_summary,
                exclude_message_id=req.exclude_message_id,
            )
        )
        return ContextBundle(
            history=[ChatTurn(role=m.role, content=m.content) for m in loaded.history],
            facts=loaded.facts,
            summary=loaded.summary,
            usage=loaded.usage,
            flags=loaded.flags,
            summary_version=loaded.summary_version,
            recent_turns=loaded.recent_turns,
        )
