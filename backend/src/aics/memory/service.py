"""memory.service: the entry points used by chat.service, eval and Celery tasks
(对话上下文管理 §0, §7.1).

- `load_context`   — MemoryPort implementation (via modules.chat.memory_port adapter);
- `capture_slots`  — per-turn slot write after the assistant message is persisted (§7.6);
- `maybe_compact`  — trigger check after each turn: online / debug enqueue the Celery task
  `aics.compact_conversation` (queue `context`), eval runs the compactor in-process (§7.1);
- `compact_now`    — forced synchronous compaction (debug "立即压缩"; jobs row in M4);
- `run_compaction_job` — Celery task body with its own engine / Redis / LLM.

Transactions: every public call opens its own unit of work and commits it; the store never
commits. Tests use `in_memory_units(store)`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any, Literal
from uuid import UUID

import structlog

from aics.memory.compactor import Compactor, CompactParams, CompactResult, uncovered_tokens
from aics.memory.context import ContextAssembler, ContextQuery, LoadedContext, TempSummarizer
from aics.memory.facts import FactCandidate, FactOutcome, reconcile, slot_candidates
from aics.memory.store import InMemoryConversationStore
from aics.memory.units import StoreFactory, Unit, in_memory_units, pg_units

__all__ = [
    "CompactParams",
    "MemoryService",
    "StoreFactory",
    "Unit",
    "in_memory_units",
    "pg_units",
    "run_compaction_job",
]

log = structlog.get_logger()

# (app_id, conversation_id, expected_version, params) → enqueue the Celery task.
CompactEnqueuer = Callable[[UUID, UUID, int, CompactParams], None]
Trigger = Literal["none", "enqueued", "ran", "no_enqueuer"]
COMPACT_TASK = "aics.compact_conversation"


class MemoryService:
    def __init__(
        self,
        units: StoreFactory,
        *,
        compactor: Compactor | None = None,
        enqueue_compaction: CompactEnqueuer | None = None,
        temp_summarizer: TempSummarizer | None = None,
    ) -> None:
        self.units = units
        self.compactor = compactor
        self.enqueue_compaction = enqueue_compaction
        if temp_summarizer is None and compactor is not None:
            temp_summarizer = compactor.temp_summary
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

    # ---- compaction (§7.1) ----
    async def needs_compaction(
        self,
        app_id: UUID,
        conversation_id: UUID,
        params: CompactParams,
        flags: Sequence[str] = (),
    ) -> tuple[bool, int]:
        """(trigger?, current summary_version)."""
        async with self.units() as u:
            st = await u.store.get_summary_state(app_id, conversation_id)
            turns = await u.store.turns_after(app_id, conversation_id, st.until_msg_id)
        uncovered = uncovered_tokens(turns, params.keep_recent_turns)
        if uncovered == 0:
            return False, st.version
        return uncovered > params.compact_trigger_tokens or "history_gap" in flags, st.version

    async def maybe_compact(
        self,
        app_id: UUID,
        conversation_id: UUID | None,
        params: CompactParams,
        *,
        run_mode: str = "online",
        flags: Sequence[str] = (),
    ) -> tuple[Trigger, CompactResult | None]:
        """Called by chat.service after the assistant message is persisted. Never raises for
        compaction problems (they only cost a later retry); stateless requests skip."""
        if conversation_id is None:
            return "none", None
        go, version = await self.needs_compaction(app_id, conversation_id, params, flags)
        if not go:
            return "none", None
        if run_mode == "eval":
            if self.compactor is None:
                raise RuntimeError("eval mode needs a compactor")
            return "ran", await self.compactor.run(
                app_id, conversation_id, params, expected_version=version
            )
        if self.enqueue_compaction is None:
            log.warning("memory.compact_no_enqueuer", conversation_id=str(conversation_id))
            return "no_enqueuer", None
        self.enqueue_compaction(app_id, conversation_id, version, params)
        return "enqueued", None

    async def compact_now(
        self, app_id: UUID, conversation_id: UUID, params: CompactParams
    ) -> CompactResult:
        """Forced compaction regardless of the trigger threshold."""
        if self.compactor is None:
            raise RuntimeError("compact_now needs a compactor")
        return await self.compactor.run(app_id, conversation_id, params)


# ---------------------------------------------------------------- Celery runtime
@asynccontextmanager
async def compaction_runtime(settings: Any = None) -> AsyncIterator[Compactor]:
    """Per-task resources: NullPool engine, Redis lock client, LLM provider."""
    import redis.asyncio as aioredis
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from aics.core.config import get_settings
    from aics.memory.locks import RedisLock
    from aics.providers import build_llm

    s = settings or get_settings()
    engine = create_async_engine(s.db_url, poolclass=NullPool)
    client = aioredis.from_url(s.redis_url)
    try:
        yield Compactor(
            pg_units(async_sessionmaker(engine, expire_on_commit=False)),
            build_llm(s),
            RedisLock(client, s.redis_prefix),
        )
    finally:
        await client.aclose()
        await engine.dispose()


async def run_compaction_job(
    app_id: UUID,
    conversation_id: UUID,
    expected_version: int,
    params: dict[str, Any],
    *,
    runtime: Callable[[], Any] | None = None,
) -> str:
    """Body of the Celery task (raises CompactionUpstreamError for autoretry)."""
    rt = runtime or compaction_runtime
    async with rt() as compactor:
        res = await compactor.run(
            app_id,
            conversation_id,
            CompactParams.model_validate(params),
            expected_version=expected_version,
        )
    return res.result
