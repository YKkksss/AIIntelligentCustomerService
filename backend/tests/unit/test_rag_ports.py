"""M2.2: rag.ports data types and protocol conformance."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from aics.modules.knowledge.chunk_search import PgChunkSearch
from aics.rag.ports import ChatTurn, ChunkSearch, ContextBundle, ContextRequest, MemoryPort


def test_pg_chunk_search_conforms() -> None:
    assert isinstance(PgChunkSearch(None), ChunkSearch)  # type: ignore[arg-type]


def test_context_request_validation() -> None:
    req = ContextRequest(
        app_id=uuid.uuid4(), conversation_id=None, question="hi", budget=3000, keep_recent_turns=4
    )
    assert req.enable_facts and not req.enable_history_recall
    with pytest.raises(ValidationError):
        ContextRequest(
            app_id=uuid.uuid4(), conversation_id=None, question="q", budget=-1, keep_recent_turns=4
        )
    with pytest.raises(ValidationError):
        ChatTurn(role="system", content="x")


async def test_memory_port_structural() -> None:
    class FakeMemory:
        async def load_context(self, req: ContextRequest) -> ContextBundle:
            return ContextBundle(history=[ChatTurn(role="user", content=req.question)])

    m = FakeMemory()
    assert isinstance(m, MemoryPort)
    b = await m.load_context(
        ContextRequest(
            app_id=uuid.uuid4(), conversation_id=None, question="q", budget=1, keep_recent_turns=1
        )
    )
    assert b.history[0].content == "q" and b.facts == {} and b.summary is None


async def test_chunk_search_validates_limits() -> None:
    s = PgChunkSearch(None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await s.keyword_search(uuid.uuid4(), "q", 101)
    with pytest.raises(ValueError):
        await s.fetch_chunks(uuid.uuid4(), [uuid.uuid4() for _ in range(501)])
