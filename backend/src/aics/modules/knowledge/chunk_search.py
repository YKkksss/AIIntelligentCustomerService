"""PostgreSQL implementation of `rag.ports.ChunkSearch` (数据库设计 §5, RAG 流程设计 §4).

Each call opens its own short session so that the vector and keyword routes of `retrieve`
can run concurrently (an AsyncSession must not be shared between concurrent tasks).
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import and_, func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aics.core.textsearch import build_tsquery
from aics.db.models import App, Chunk, ChunkParent, Document
from aics.rag.ports import ChunkText, KeywordHit, ParentText

MAX_KEYWORD_K = 100
MAX_FETCH_IDS = 500


def retrievable(tags: Sequence[str] | None = None) -> list[object]:
    """The single definition of "retrievable chunk" (数据库设计 §3.7); `tags` adds the
    request's any-match tag filter (M2.2b).

    M4.3b adds the validity window: a document outside `effective_at` / `expires_at` is not
    retrievable. Time lives only in PG — Qdrant keeps no timestamps, so vector hits are
    dropped when their rows are fetched back (ISSUE-96).
    """
    cond: list[object] = [
        Chunk.enabled,
        Chunk.review_status != "flagged",
        Chunk.index_version == Document.index_version,
        Document.status != "deleting",
        Document.deleted_at.is_(None),
        or_(Document.effective_at.is_(None), Document.effective_at <= func.now()),
        or_(Document.expires_at.is_(None), Document.expires_at > func.now()),
    ]
    if tags:
        cond.append(Document.tags.overlap(list(tags)))
    return cond


class PgChunkSearch:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def _custom_dict(self, s: AsyncSession, app_id: UUID) -> list[str]:
        return list(await s.scalar(select(App.custom_dict).where(App.id == app_id)) or [])

    async def keyword_search(
        self, app_id: UUID, question: str, k: int, *, tags: Sequence[str] | None = None
    ) -> list[KeywordHit]:
        if not 1 <= k <= MAX_KEYWORD_K:
            raise ValueError(f"k must be within 1..{MAX_KEYWORD_K}")
        async with self.sessions() as s:
            tsq = build_tsquery(app_id, question, await self._custom_dict(s, app_id))
            if not tsq:
                return []
            q = func.to_tsquery(literal_column("'simple'::regconfig"), tsq)
            score = func.ts_rank_cd(Chunk.tsv, q).label("score")
            stmt = (
                select(Chunk.id, score)
                .join(Document, Document.id == Chunk.document_id)
                .where(Chunk.app_id == app_id, Chunk.tsv.op("@@")(q), *retrievable(tags))
                .order_by(score.desc(), Chunk.id)
                .limit(k)
            )
            rows = (await s.execute(stmt)).all()
        return [KeywordHit(r[0], float(r[1])) for r in rows]

    async def fetch_chunks(
        self, app_id: UUID, chunk_ids: Sequence[UUID], *, tags: Sequence[str] | None = None
    ) -> dict[UUID, ChunkText]:
        ids = list(dict.fromkeys(chunk_ids))
        if not ids:
            return {}
        if len(ids) > MAX_FETCH_IDS:
            raise ValueError(f"at most {MAX_FETCH_IDS} ids per call")
        stmt = (
            select(Chunk, Document.title)
            .join(Document, Document.id == Chunk.document_id)
            .where(Chunk.app_id == app_id, Chunk.id.in_(ids), *retrievable(tags))
        )
        async with self.sessions() as s:
            rows = (await s.execute(stmt)).all()
        return {
            c.id: ChunkText(
                chunk_id=c.id,
                document_id=c.document_id,
                document_title=title,
                heading_path=c.heading_path,
                text=c.text,
                page_from=c.page_from,
                parent_id=c.parent_id,
            )
            for c, title in rows
        }

    async def fetch_parents(
        self, app_id: UUID, parent_ids: Sequence[UUID]
    ) -> dict[UUID, ParentText]:
        ids = list(dict.fromkeys(parent_ids))
        if not ids:
            return {}
        if len(ids) > MAX_FETCH_IDS:
            raise ValueError(f"at most {MAX_FETCH_IDS} ids per call")
        stmt = (
            select(ChunkParent, Document.title)
            .join(
                Document,
                and_(
                    Document.id == ChunkParent.document_id,
                    Document.index_version == ChunkParent.index_version,
                ),
            )
            .where(ChunkParent.app_id == app_id, ChunkParent.id.in_(ids))
        )
        async with self.sessions() as s:
            rows = (await s.execute(stmt)).all()
        return {
            p.id: ParentText(
                parent_id=p.id,
                document_id=p.document_id,
                document_title=title,
                heading_path=p.heading_path,
                text=p.text,
                page_from=p.page_from,
            )
            for p, title in rows
        }
