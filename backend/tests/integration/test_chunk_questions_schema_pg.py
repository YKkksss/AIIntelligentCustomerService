"""M2.2b: migration 0008 constraints (documents.tags, chunk_questions)."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from aics.db.models import Chunk, ChunkQuestion, Document
from aics.ingest.repository import ChunkQuestionRepository

pytestmark = pytest.mark.integration


async def _doc_chunk(s: Any, app_id: Any) -> tuple[Document, Chunk]:
    d = Document(
        app_id=app_id, title="t", source_type="file", content_hash="a" * 64, tags=["x", "y"]
    )
    s.add(d)
    await s.flush()
    c = Chunk(
        app_id=app_id, document_id=d.id, index_version=1, ordinal=0, text="正文", search_text="正文"
    )
    s.add(c)
    await s.flush()
    return d, c


def _q(app_id: Any, d: Document, c: Chunk | None, **kw: Any) -> ChunkQuestion:
    return ChunkQuestion(
        app_id=app_id,
        document_id=d.id,
        chunk_id=c.id if c else None,
        question=kw.pop("q", "专票怎么开"),
        anchor_ordinal=0,
        anchor_hash=hashlib.sha256(b"x").hexdigest(),
        **kw,
    )


async def test_tags_default_and_question_roundtrip(pg: dict[str, Any]) -> None:
    app = pg["app_a"]
    async with pg["sessions"]() as s:
        d, c = await _doc_chunk(s, app)
        bare = Document(app_id=app, title="b", source_type="file", content_hash="b" * 64)
        s.add(bare)
        q = await ChunkQuestionRepository(s).add(app, _q(app, d, c))
        await s.commit()
        assert (await s.get(Document, bare.id)).tags == []
        assert (await s.get(Document, d.id)).tags == ["x", "y"]
        repo = ChunkQuestionRepository(s)
        assert [r.id for r in await repo.list_for_chunk(app, c.id)] == [q.id]
        assert await repo.list_for_chunk(pg["app_b"], c.id) == []  # scoped
        assert q.status == "active" and q.source == "manual"
        # chunk deleted (old build cleanup) → question survives, chunk_id NULL
        await s.execute(update_pending(q.id))
        await s.execute(delete(Chunk).where(Chunk.id == c.id))
        await s.commit()
        await s.refresh(q)
        assert q.chunk_id is None
        # document deleted → questions cascade
        await s.execute(delete(Document).where(Document.id == d.id))
        await s.commit()
        assert (
            await s.execute(select(ChunkQuestion).where(ChunkQuestion.id == q.id))
        ).first() is None
        await s.execute(delete(Document).where(Document.id == bare.id))
        await s.commit()


def update_pending(qid: Any) -> Any:
    from sqlalchemy import update

    return update(ChunkQuestion).where(ChunkQuestion.id == qid).values(status="pending")


@pytest.mark.parametrize(
    "kw",
    [{"q": ""}, {"q": "长" * 201}, {"status": "bogus"}, {"source": "x"}],
)
async def test_question_constraints(pg: dict[str, Any], kw: dict[str, Any]) -> None:
    app = pg["app_a"]
    async with pg["sessions"]() as s:
        d, c = await _doc_chunk(s, app)
        did = d.id
        await s.commit()
        s.add(_q(app, d, c, **kw))
        with pytest.raises(IntegrityError):
            await s.commit()
        await s.rollback()
        await s.execute(delete(Document).where(Document.id == did))
        await s.commit()
