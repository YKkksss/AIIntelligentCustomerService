"""M4.3b ③：切块级运营的业务规则（编辑 / 新增 / 删除 / 近重复 / 有效期）。

路由协议层在 `test_knowledge_chunk_ops_api.py`；这里盯 service：
正文编辑只重算本块、手工块来源与序号、删除时的向量点与审计、SimHash 只提示不阻止、
有效期必须是有效区间。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from aics.core.audit import AuditEntry
from aics.core.errors import ValidationAppError
from aics.db.models import App, Chunk, Document
from aics.modules.knowledge.service import KnowledgeService

APP_ID = uuid4()
DOC_ID = uuid4()
CHUNK_ID = uuid4()
OWNER = uuid4()


class FakeResult:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[Any, ...]]:
        return self._rows


class FakeSession:
    def __init__(self, *, next_ordinal: int = 0, rows: list[tuple[Any, ...]] | None = None) -> None:
        self.commits = 0
        self.refreshed: list[Any] = []
        self.added: list[Any] = []
        self.deleted: list[Any] = []
        self.flushes = 0
        self.next_ordinal = next_ordinal
        self.rows = rows or []

    async def get(self, model: Any, pk: Any) -> Any:
        assert model is App
        app = App(id=APP_ID, name="客服", status="active")
        app.custom_dict = ["退货"]
        app.chunk_config = {"contextual_prefix": True}
        return app

    async def scalar(self, _stmt: Any) -> Any:
        return self.next_ordinal

    async def execute(self, _stmt: Any) -> FakeResult:
        return FakeResult(self.rows)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def delete(self, obj: Any) -> None:
        self.deleted.append(obj)

    async def flush(self) -> None:
        self.flushes += 1

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, obj: Any) -> None:
        self.refreshed.append(obj)


class FakeStore:
    def __init__(self) -> None:
        self.deleted: list[tuple[Any, list[Any]]] = []

    async def delete_points(self, app_id: Any, point_ids: list[Any]) -> None:
        self.deleted.append((app_id, list(point_ids)))


class FakeSink:
    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    async def write(self, session: Any, entry: AuditEntry) -> None:
        self.entries.append(entry)


def _document(**kw: Any) -> Document:
    fields: dict[str, Any] = {
        "id": DOC_ID,
        "app_id": APP_ID,
        "title": "退货政策.md",
        "source_type": "file",
        "mime_type": "text/markdown",
        "size_bytes": 100,
        "status": "done",
        "progress": 100,
        "chunk_count": 2,
        "index_version": 1,
        "tags": ["售后"],
        "chunk_config": {},
        "simhash": None,
    }
    fields.update(kw)
    return Document(**fields)


def _chunk(**kw: Any) -> Chunk:
    fields: dict[str, Any] = {
        "id": CHUNK_ID,
        "app_id": APP_ID,
        "document_id": DOC_ID,
        "index_version": 1,
        "ordinal": 0,
        "heading_path": "第一章",
        "text": "自签收之日起 7 天内可退货。",
        "tokens": 12,
        "enabled": True,
        "review_status": "none",
        "source": "parsed",
    }
    fields.update(kw)
    return Chunk(**fields)


class Service(KnowledgeService):
    def __init__(
        self,
        session: FakeSession,
        *,
        chunk: Chunk | None = None,
        doc: Document | None = None,
        **kw: Any,
    ) -> None:
        super().__init__(session, storage=object(), **kw)  # type: ignore[arg-type]
        self._chunk = chunk or _chunk()
        self._document = doc or _document()
        self.reembedded: list[Any] = []

    async def _live_chunk(self, app_id: Any, chunk_id: Any) -> tuple[Chunk, Document]:
        return self._chunk, self._document

    async def _doc(self, app_id: Any, document_id: Any, *, lock: bool = False) -> Document:
        return self._document

    async def _reembed_chunk(self, app_id: Any, chunk: Chunk, doc: Document) -> None:
        self.reembedded.append((chunk.id, chunk.text))


@pytest.mark.asyncio
async def test_edit_text_marks_chunk_edited_and_reembeds_only_it() -> None:
    session, sink = FakeSession(), FakeSink()
    service = Service(session, audit=sink)  # type: ignore[arg-type]

    chunk = await service.update_chunk(
        APP_ID, CHUNK_ID, text="签收后 15 天内可退货。", created_by=OWNER
    )

    assert chunk.source == "edited"
    assert chunk.text == "签收后 15 天内可退货。"
    assert chunk.tokens > 0
    assert chunk.search_text  # 重算了分词文本
    assert service.reembedded == [(CHUNK_ID, "签收后 15 天内可退货。")]
    assert session.commits == 1
    entry = sink.entries[0]
    assert entry.action == "kb.chunk.update" and entry.target_type == "chunk"
    assert entry.diff["after"]["source"] == "edited"


@pytest.mark.asyncio
async def test_edit_with_blank_text_is_rejected() -> None:
    service = Service(FakeSession())

    with pytest.raises(ValidationAppError) as excinfo:
        await service.update_chunk(APP_ID, CHUNK_ID, text="   ")

    errors = excinfo.value.details["errors"]
    assert errors[0]["loc"] == ["text"]


@pytest.mark.asyncio
async def test_edit_without_changes_does_not_commit() -> None:
    session = FakeSession()
    chunk = _chunk()
    service = Service(session, chunk=chunk)

    result = await service.update_chunk(APP_ID, CHUNK_ID, text=chunk.text)

    assert result is chunk
    assert session.commits == 0 and service.reembedded == []


@pytest.mark.asyncio
async def test_add_chunk_uses_next_ordinal_and_manual_source() -> None:
    session, sink = FakeSession(next_ordinal=7), FakeSink()
    service = Service(session, audit=sink)  # type: ignore[arg-type]

    chunk = await service.add_chunk(
        APP_ID, DOC_ID, text="加急订单 24 小时内发货。", created_by=OWNER
    )

    assert chunk.source == "manual"
    assert chunk.ordinal == 7
    assert chunk.index_version == 1
    assert session.added == [chunk] and session.flushes == 1 and session.commits == 1
    assert sink.entries[0].action == "kb.chunk.create"
    assert service.reembedded == [(chunk.id, "加急订单 24 小时内发货。")]


@pytest.mark.asyncio
async def test_delete_chunk_removes_points_and_audits() -> None:
    session, store, sink = FakeSession(), FakeStore(), FakeSink()
    service = Service(session, store=store, audit=sink)  # type: ignore[arg-type]  # type: ignore[arg-type]

    await service.delete_chunk(APP_ID, CHUNK_ID, created_by=OWNER)

    assert store.deleted == [(APP_ID, [CHUNK_ID])]
    assert session.deleted and session.commits == 1
    assert sink.entries[0].action == "kb.chunk.delete"


@pytest.mark.asyncio
async def test_duplicates_reports_only_near_duplicates() -> None:
    same = 0b1010_1010
    rows = [
        (uuid4(), "几乎一样.md", same),
        (uuid4(), "完全无关.md", 0b0101_0101_0101_0101),
        (uuid4(), "没指纹.md", None),
    ]
    session = FakeSession(rows=rows)
    service = Service(session, doc=_document(simhash=same))

    found = await service.duplicates(APP_ID, DOC_ID)

    assert [item["title"] for item in found] == ["几乎一样.md"]
    assert found[0]["distance"] == 0


@pytest.mark.asyncio
async def test_duplicates_without_simhash_returns_empty() -> None:
    service = Service(FakeSession(), doc=_document(simhash=None))

    assert await service.duplicates(APP_ID, DOC_ID) == []


@pytest.mark.asyncio
async def test_document_validity_window_is_validated() -> None:
    service = Service(FakeSession())
    start = datetime(2026, 10, 1, tzinfo=UTC)
    end = datetime(2026, 9, 1, tzinfo=UTC)

    with pytest.raises(ValidationAppError) as excinfo:
        await service.update_document(APP_ID, DOC_ID, effective_at=start, expires_at=end)

    assert excinfo.value.details["errors"][0]["loc"] == ["expires_at"]


@pytest.mark.asyncio
async def test_document_validity_keeps_untouched_field() -> None:
    doc = _document(effective_at=datetime(2026, 10, 1, tzinfo=UTC))
    session, sink = FakeSession(), FakeSink()
    service = Service(session, doc=doc, audit=sink)  # type: ignore[arg-type]

    await service.update_document(
        APP_ID, DOC_ID, expires_at=datetime(2026, 12, 31, tzinfo=UTC), created_by=OWNER
    )

    assert doc.expires_at == datetime(2026, 12, 31, tzinfo=UTC)
    assert doc.effective_at == datetime(2026, 10, 1, tzinfo=UTC)  # 没传就不动
    diff = sink.entries[0].diff
    assert diff["after"]["expires_at"].startswith("2026-12-31")
