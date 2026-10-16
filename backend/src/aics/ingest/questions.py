"""Supplementary chunk questions: embedding and rebuild migration (M2.2b, 设计讨论 0003).

- A question is embedded *alone* (no title prefix: it must look like a user question) and
  stored as its own Qdrant point (id = question id, payload `kind=question`, `chunk_id`).
- A rebuild creates new chunk ids. After the build switches, `migrate_document_questions`
  re-attaches every active question of the document to the new build's chunk whose text
  hash equals the question's `anchor_hash` (nearest `anchor_ordinal` wins on duplicates).
  Unmatched questions become `pending` (chunk_id NULL, point deleted) for an admin to
  re-attach. The step is idempotent and also runs before old-build cleanup, so cleanup's
  chunk deletion never meets an active question still pointing at an old chunk.
- PG is the source of truth; Qdrant payload moves are best effort (a stale point maps to a
  non-retrievable chunk and is dropped by the PG re-check, i.e. a miss, never a wrong hit).
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aics.db.models import Chunk, ChunkQuestion, Document
from aics.providers.base import EmbeddingProvider
from aics.vectorstore import ChunkPoint, QdrantVectorStore

log = structlog.get_logger()


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class QuestionTarget:
    """What a question point needs to know about its chunk."""

    chunk_id: UUID
    document_id: UUID
    index_version: int
    parent_id: UUID | None
    enabled: bool  # chunk.enabled AND not flagged
    tags: Sequence[str]


async def upsert_question_points(
    app_id: UUID,
    rows: Sequence[tuple[UUID, str]],  # (question_id, question)
    target: QuestionTarget,
    *,
    embedder: EmbeddingProvider,
    store: QdrantVectorStore,
) -> None:
    if not rows:
        return
    vectors = await embedder.embed_documents([q for _, q in rows])
    await store.upsert_chunks(
        app_id,
        [
            ChunkPoint(
                chunk_id=target.chunk_id,
                document_id=target.document_id,
                index_version=target.index_version,
                vector=v,
                enabled=target.enabled,
                parent_id=target.parent_id,
                tags=target.tags,
                kind="question",
                point_id=qid,
            )
            for (qid, _), v in zip(rows, vectors, strict=True)
        ],
    )


@dataclass
class MigrationReport:
    moved: int = 0
    pending: int = 0


async def _plan(
    s: AsyncSession, app_id: UUID, document_id: UUID
) -> tuple[list[tuple[ChunkQuestion, Chunk]], list[ChunkQuestion]]:
    doc = await s.get(Document, document_id)
    if doc is None or doc.app_id != app_id:
        return [], []
    live = doc.index_version
    stmt = (
        select(ChunkQuestion, Chunk.index_version)
        .outerjoin(Chunk, Chunk.id == ChunkQuestion.chunk_id)
        .where(
            ChunkQuestion.app_id == app_id,
            ChunkQuestion.document_id == document_id,
            ChunkQuestion.status == "active",
        )
        .with_for_update(of=ChunkQuestion)
    )
    stale = [q for q, ver in (await s.execute(stmt)).all() if ver is None or ver != live]
    if not stale:
        return [], []
    chunks = (
        await s.execute(
            select(Chunk).where(
                Chunk.app_id == app_id,
                Chunk.document_id == document_id,
                Chunk.index_version == live,
            )
        )
    ).scalars()
    by_hash: dict[str, list[Chunk]] = defaultdict(list)
    for c in chunks:
        by_hash[text_hash(c.text)].append(c)
    moved: list[tuple[ChunkQuestion, Chunk]] = []
    lost: list[ChunkQuestion] = []
    for q in stale:
        cands = by_hash.get(q.anchor_hash)
        if not cands:
            lost.append(q)
            continue
        best = min(cands, key=lambda c: (abs(c.ordinal - q.anchor_ordinal), c.ordinal))
        moved.append((q, best))
    return moved, lost


async def migrate_document_questions(
    sessions: async_sessionmaker[AsyncSession],
    store: QdrantVectorStore | None,
    app_id: UUID,
    document_id: UUID,
) -> MigrationReport:
    """Re-attach the document's active questions to its live build (idempotent)."""
    async with sessions() as s:
        moved, lost = await _plan(s, app_id, document_id)
        if not moved and not lost:
            return MigrationReport()
        tags = list((await s.get(Document, document_id)).tags or [])  # type: ignore[union-attr]
        moves = []
        for q, c in moved:
            q.chunk_id, q.anchor_ordinal = c.id, c.ordinal
            moves.append((q.id, c))
        lost_ids = [q.id for q in lost]
        for q in lost:
            q.chunk_id, q.status = None, "pending"
        await s.commit()
    if store is not None:
        try:
            for qid, c in moves:
                await store.move_question(
                    app_id,
                    qid,
                    chunk_id=c.id,
                    index_version=c.index_version,
                    enabled=c.enabled and c.review_status != "flagged",
                    parent_id=c.parent_id,
                )
            await store.delete_points(app_id, lost_ids)
            if moves:
                await store.set_document_tags(app_id, document_id, tags)
        except Exception:  # noqa: BLE001 - PG already consistent; stale points only miss
            log.exception("questions.qdrant_sync_failed", document_id=str(document_id))
    report = MigrationReport(moved=len(moves), pending=len(lost_ids))
    log.info(
        "questions.migrated",
        document_id=str(document_id),
        moved=report.moved,
        pending=report.pending,
    )
    return report
