"""memory.service: the entry points used by chat.service, eval and Celery tasks
(对话上下文管理 §0).

Transactions: every public call opens one unit of work (`StoreFactory`) and commits it
itself; the store never commits. Tests use `in_memory_units(store)`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from aics.memory.context import ContextAssembler, ContextQuery, LoadedContext, TempSummarizer
from aics.memory.facts import FactCandidate, FactOutcome, reconcile, slot_candidates
from aics.memory.store import ConversationStore, InMemoryConversationStore


@dataclass
class Unit:
    """One transaction over a ConversationStore."""

    store: ConversationStore
    session: Any = None  # AsyncSession for PG; None in memory

    async def commit(self) -> None:
        if self.session is not None:
            await self.session.commit()

    async def rollback(self) -> None:
        if self.session is not None:
            await self.session.rollback()


StoreFactory = Callable[[], AbstractAsyncContextManager[Unit]]


def pg_units(sessions: Any) -> StoreFactory:
    """Units over an `async_sessionmaker` (PgConversationStore per session)."""
    from aics.memory.repository import PgConversationStore

    @asynccontextmanager
    async def unit() -> AsyncIterator[Unit]:
        async with sessions() as session:
            u = Unit(PgConversationStore(session), session)
            try:
                yield u
            except BaseException:
                await session.rollback()
                raise

    return unit


def in_memory_units(store: InMemoryConversationStore) -> StoreFactory:
    @asynccontextmanager
    async def unit() -> AsyncIterator[Unit]:
        yield Unit(store)

    return unit


class MemoryService:
    def __init__(
        self, units: StoreFactory, *, temp_summarizer: TempSummarizer | None = None
    ) -> None:
        self.units = units
        self.temp_summarizer = temp_summarizer

    async def load_context(self, q: ContextQuery) -> LoadedContext:
        """Read-only assembly (§6.2); stateless requests never touch the database."""
        if q.conversation_id is None:
            return await ContextAssembler(
                InMemoryConversationStore(), temp_summarizer=self.temp_summarizer
            ).load(q)
        async with self.units() as u:
            return await ContextAssembler(u.store).load(q)

    async def capture_slots(
        self,
        app_id: UUID,
        conversation_id: UUID,
        slots: dict[str, str | None] | None,
        *,
        intent_source: str,
        intent_confidence: float,
        user_message_id: UUID | None,
        enable_facts: bool = True,
    ) -> list[FactOutcome]:
        """Per-turn slot write (§7.6), called by chat.service after the assistant message is
        persisted. Own transaction; errors propagate (the caller logs and continues)."""
        if not enable_facts:
            return []
        cands = slot_candidates(
            slots,
            intent_source=intent_source,
            intent_confidence=intent_confidence,
            user_message_id=user_message_id,
        )
        return await self.write_facts(app_id, conversation_id, cands)

    async def write_facts(
        self, app_id: UUID, conversation_id: UUID, candidates: list[FactCandidate]
    ) -> list[FactOutcome]:
        if not candidates:
            return []
        async with self.units() as u:
            out = await reconcile(u.store, app_id, conversation_id, candidates)
            await u.commit()
        return out
