"""M4.3 ③：切块复核与启停（`KnowledgeService.update_chunk` / `list_app_chunks`）。

协议层（路由参数与序列化）由 `test_knowledge_admin_api.py` 覆盖；这里盯业务规则：
复核通过恢复可检索、无变化不提交、审计目标类型为 chunk、Qdrant 同步失败不影响应答。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

import pytest

from aics.core.audit import AuditEntry
from aics.db.models import App, Chunk, Document
from aics.modules.knowledge.service import AppChunkPage, KnowledgeService

APP_ID = uuid4()
DOC_ID = uuid4()
CHUNK_ID = uuid4()


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.refreshed: list[Any] = []

    async def get(self, model: Any, pk: Any) -> Any:
        assert model is App
        return App(id=APP_ID, name="客服", status="active")

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, obj: Any) -> None:
        self.refreshed.append(obj)


class FakeStorage:
    async def get(self, key: str) -> bytes:  # pragma: no cover - 用例用不到
        return b""

    async def put(self, key: str, data: bytes) -> None:  # pragma: no cover
        return None

    async def exists(self, key: str) -> bool:  # pragma: no cover
        return False

    async def delete(self, key: str) -> bool:  # pragma: no cover
        return True

    async def delete_prefix(self, prefix: str) -> int:  # pragma: no cover
        return 0


class FakeStore:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[Any, list[Any], bool]] = []

    async def set_enabled(self, app_id: Any, chunk_ids: list[Any], enabled: bool) -> None:
        self.calls.append((app_id, list(chunk_ids), enabled))
        if self.fail:
            raise RuntimeError("qdrant down")


class FakeSink:
    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    async def write(self, session: Any, entry: AuditEntry) -> None:
        self.entries.append(entry)


class FakeChunks:
    def __init__(self, rows: list[tuple[Chunk, str]]) -> None:
        self.rows = rows
        self.kwargs: dict[str, Any] = {}

    async def page_for_app(self, app_id: Any, **kw: Any) -> tuple[list[tuple[Chunk, str]], int]:
        self.kwargs = kw
        return self.rows, len(self.rows)


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
    }
    fields.update(kw)
    return Chunk(**fields)


class Service(KnowledgeService):
    """把 `_live_chunk` 换成固定切块，让用例只测 `update_chunk` 的规则。"""

    def __init__(self, session: FakeSession, chunk: Chunk, **kw: Any) -> None:
        super().__init__(session, storage=FakeStorage(), **kw)  # type: ignore[arg-type]
        self._chunk = chunk

    async def _live_chunk(self, app_id: Any, chunk_id: Any) -> tuple[Chunk, Document]:
        return self._chunk, Document(
            id=DOC_ID,
            app_id=APP_ID,
            title="退货政策.md",
            source_type="file",
            mime_type="text/markdown",
            size_bytes=1,
            status="done",
            progress=1,
            chunk_count=1,
            index_version=1,
            tags=[],
        )


@pytest.mark.asyncio
async def test_disable_syncs_payload_and_audits_chunk() -> None:
    session, store, sink = FakeSession(), FakeStore(), FakeSink()
    chunk = _chunk()
    service = Service(session, chunk, store=store, audit=sink)  # type: ignore[arg-type]

    result = await service.update_chunk(APP_ID, CHUNK_ID, enabled=False, created_by=uuid4())

    assert result.enabled is False
    assert session.commits == 1 and session.refreshed == [chunk]
    assert store.calls == [(APP_ID, [CHUNK_ID], False)]
    entry = sink.entries[0]
    assert entry.action == "kb.chunk.update" and entry.target_type == "chunk"
    assert entry.target_id == CHUNK_ID
    assert entry.diff == {"before": {"enabled": True}, "after": {"enabled": False}}


@pytest.mark.asyncio
async def test_approve_restores_retrievable_and_records_reviewer() -> None:
    session, store, sink = FakeSession(), FakeStore(), FakeSink()
    chunk = _chunk(enabled=False, review_status="flagged", flag_reason="疑似恶意指令")
    service = Service(session, chunk, store=store, audit=sink)  # type: ignore[arg-type]
    reviewer = uuid4()

    result = await service.update_chunk(
        APP_ID, CHUNK_ID, review_status="approved", created_by=reviewer
    )

    assert result.review_status == "approved"
    assert result.enabled is True
    assert result.reviewed_by == reviewer
    assert isinstance(result.reviewed_at, datetime)
    assert store.calls == [(APP_ID, [CHUNK_ID], True)]
    assert sink.entries[0].diff == {
        "before": {"review_status": "flagged", "enabled": False},
        "after": {"review_status": "approved", "enabled": True},
    }


@pytest.mark.asyncio
async def test_no_change_is_a_noop() -> None:
    session, store, sink = FakeSession(), FakeStore(), FakeSink()
    service = Service(session, _chunk(), store=store, audit=sink)  # type: ignore[arg-type]

    await service.update_chunk(APP_ID, CHUNK_ID, enabled=True, created_by=uuid4())

    assert session.commits == 0 and sink.entries == [] and store.calls == []


@pytest.mark.asyncio
async def test_vector_sync_failure_does_not_break_the_call() -> None:
    session = FakeSession()
    service = Service(session, _chunk(), store=FakeStore(fail=True))  # type: ignore[arg-type]

    result = await service.update_chunk(APP_ID, CHUNK_ID, enabled=False, created_by=uuid4())

    assert result.enabled is False and session.commits == 1


@pytest.mark.asyncio
async def test_rejects_unknown_review_status() -> None:
    service = Service(FakeSession(), _chunk())  # type: ignore[arg-type]

    with pytest.raises(Exception) as info:
        await service.update_chunk(APP_ID, CHUNK_ID, review_status="flagged", created_by=uuid4())

    assert "只接受 approved" in str(info.value)


@pytest.mark.asyncio
async def test_list_app_chunks_builds_rows_with_document_title() -> None:
    session = FakeSession()
    chunk = _chunk(review_status="flagged", flag_reason="疑似恶意指令")
    service = Service(session, chunk)  # type: ignore[arg-type]
    fake_chunks = FakeChunks([(chunk, "退货政策.md")])
    service.chunks = fake_chunks  # type: ignore[assignment]

    page: AppChunkPage = await service.list_app_chunks(
        APP_ID, review_status="flagged", enabled=True, page=2, page_size=5
    )

    assert (page.total, page.page, page.page_size) == (1, 2, 5)
    item = page.items[0]
    assert item["document_title"] == "退货政策.md"
    assert item["review_status"] == "flagged" and item["flag_reason"] == "疑似恶意指令"
    assert item["reviewed_at"] is None
    assert fake_chunks.kwargs == {
        "document_id": None,
        "review_status": "flagged",
        "enabled": True,
        "limit": 5,
        "offset": 5,
    }


@pytest.mark.asyncio
async def test_list_app_chunks_rejects_deleting_app() -> None:
    class DeletingSession(FakeSession):
        async def get(self, model: Any, pk: Any) -> Any:
            return App(id=APP_ID, name="客服", status="deleting")

    service = Service(DeletingSession(), _chunk())  # type: ignore[arg-type]

    with pytest.raises(Exception) as info:
        await service.list_app_chunks(APP_ID)

    assert "APP_NOT_FOUND" in str(info.value) or "应用不存在" in str(info.value)
