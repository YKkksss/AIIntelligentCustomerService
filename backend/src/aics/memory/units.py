"""Units of work over a ConversationStore (one transaction each)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any

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
