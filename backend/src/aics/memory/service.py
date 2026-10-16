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

from aics.memory.context import ContextAssembler, ContextQuery, LoadedContext, TempSummarizer
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
