"""Ports through which aics.rag gets data (模块划分 §3, ADR-0008).

rag never imports aics.db / aics.modules / aics.memory. Implementations:
- `ChunkSearch` → `aics.modules.knowledge.chunk_search.PgChunkSearch`
- `MemoryPort`  → `aics.memory` (M2.5; structural typing, memory does not import rag)
- `RagPorts.llm / vector / reranker` → `aics.providers` + `rag.retrieval.EmbeddingVectorSearch`
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from aics.providers.base import LLMProvider, RerankProvider
    from aics.rag.retrieval import VectorSearch


# ---------------------------------------------------------------- knowledge
@dataclass(frozen=True, slots=True)
class KeywordHit:
    chunk_id: UUID
    score: float  # ts_rank_cd; only comparable within one query


@dataclass(frozen=True, slots=True)
class ChunkText:
    """A retrievable child chunk with the fields needed for rerank / generate / citations."""

    chunk_id: UUID
    document_id: UUID
    document_title: str
    heading_path: str | None
    text: str
    page_from: int | None
    parent_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ParentText:
    parent_id: UUID
    document_id: UUID
    document_title: str
    heading_path: str | None
    text: str
    page_from: int | None


@runtime_checkable
class ChunkSearch(Protocol):
    async def keyword_search(
        self, app_id: UUID, question: str, k: int, *, tags: Sequence[str] | None = None
    ) -> list[KeywordHit]:
        """Full-text search over retrievable chunks of the app (OR tsquery, ts_rank_cd desc).

        `tags`: only documents having any of them (None / empty = no filter, M2.2b).

        Returns [] when the question has no usable terms (caller skips the keyword route
        without recording degraded)."""
        ...

    async def fetch_chunks(
        self, app_id: UUID, chunk_ids: Sequence[UUID], *, tags: Sequence[str] | None = None
    ) -> dict[UUID, ChunkText]:
        """Only chunks that are retrievable *now* (enabled, not flagged, current index version,
        document not deleting, and matching `tags` when given); others are silently omitted."""
        ...

    async def fetch_parents(
        self, app_id: UUID, parent_ids: Sequence[UUID]
    ) -> dict[UUID, ParentText]: ...


# ---------------------------------------------------------------- memory
class ChatTurn(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: Literal["user", "assistant", "human_agent"]
    content: str


class RecalledSnippet(BaseModel):
    message_id: UUID
    role: str
    content: str
    score: float


class ContextRequest(BaseModel):
    """对话上下文管理 §0."""

    app_id: UUID
    conversation_id: UUID | None
    inline_history: list[ChatTurn] | None = None
    question: str
    budget: int = Field(ge=0)
    keep_recent_turns: int = Field(ge=0)
    enable_facts: bool = True
    enable_history_recall: bool = False
    temp_summary: bool = True
    # Current user message if chat.service persisted it before generation (excluded).
    exclude_message_id: UUID | None = None


class ContextBundle(BaseModel):
    history: list[ChatTurn] = Field(default_factory=list)
    facts: dict[str, str] = Field(default_factory=dict)
    # Structured summary as a plain dict ({user_goal, confirmed_info, ...}); rag does not
    # import memory's ConversationSummary model.
    summary: dict[str, Any] | None = None
    recalled: list[RecalledSnippet] = Field(default_factory=list)
    usage: dict[str, int] = Field(default_factory=dict)
    flags: list[str] = Field(default_factory=list)
    summary_version: int = 0
    recent_turns: int = 0


@runtime_checkable
class MemoryPort(Protocol):
    async def load_context(self, req: ContextRequest) -> ContextBundle: ...


@dataclass
class RagPorts:
    """Everything run_rag needs from the outside world (built by chat.service / eval).

    `llm` is required by run_rag; `vector` may be None only for `retrieval_mode="fulltext"`;
    `reranker` None (or a noop reranker) means judge falls back to the LLM.
    """

    chunk_search: ChunkSearch
    memory: MemoryPort | None = None
    llm: LLMProvider | None = None
    vector: VectorSearch | None = None
    reranker: RerankProvider | None = None
    extras: dict[str, Any] = field(default_factory=dict)
