"""M3.2 ②：知识库开放接口（读）—— 路由参数、序列化与错误码（开放接口参考 §4 / 接口规范 §5）。

用 fake 认证器 + fake 运行时跑真路由（`create_app()` 挂载），不需要数据库；
数据库行为由 `tests/integration/test_isolation_pg.py` 与 `test_knowledge_*.py` 覆盖。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, get_args
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from aics.core.errors import DocumentNotFoundError, ForbiddenError
from aics.core.idempotency import Idempotency
from aics.core.security import Principal
from aics.db.models import Chunk, Document, Job
from aics.db.models.knowledge import DOCUMENT_STATUSES
from aics.ingest.repository import like_pattern
from aics.main import create_app
from aics.modules.knowledge import router as knowledge_router
from aics.modules.knowledge import runtime as knowledge_runtime
from aics.modules.knowledge.router import DocumentStatus
from aics.modules.knowledge.runtime import KnowledgeRuntime
from aics.modules.knowledge.service import (
    KEEP,
    PAGE_MAX,
    ChunkPage,
    DocumentBusyError,
    DocumentPage,
    ReplaceResult,
    UploadResult,
    page_args,
)
from tests.fake_redis import FakeRedis


class FakeAuth:
    """只校验 scope 是否被请求；真实校验由 tests/unit/test_apikeys.py 覆盖。"""

    def __init__(self, scopes: tuple[str, ...] = ("kb:read",)) -> None:
        self.scopes = scopes
        # 幂等键按 key_id 分桶：同一个 Key 的多次请求必须拿到同一个 key_id
        self.key_id = uuid4()
        self.calls: list[tuple[UUID, tuple[str, ...]]] = []

    async def authenticate(
        self,
        authorization: str | None,
        *,
        app_id: UUID,
        any_scope: tuple[str, ...],
        client_ip: str | None = None,
        forwarded_for: str | None = None,
    ) -> Principal:
        self.calls.append((app_id, any_scope))
        if not set(self.scopes) & set(any_scope):
            raise ForbiddenError(details={"required_scope": list(any_scope)})
        return Principal(
            app_id=app_id,
            key_id=self.key_id,
            key_prefix="aics_test",
            scopes=frozenset(self.scopes),
            rate_headers={"X-RateLimit-Limit": "60", "X-RateLimit-Remaining": "59"},
        )


class FakeService:
    def __init__(
        self,
        *,
        documents: list[Document] | None = None,
        chunks: list[Chunk] | None = None,
        total: int | None = None,
        not_found: bool = False,
        duplicate: bool = False,
        unchanged: bool = False,
        busy: bool = False,
    ) -> None:
        self.documents = list(documents or [])
        self.chunks = list(chunks or [])
        self.total = total
        self.not_found = not_found
        self.duplicate = duplicate
        self.unchanged = unchanged
        self.busy = busy
        self.calls: list[tuple[Any, ...]] = []

    async def list_documents(self, app_id: UUID, **kw: Any) -> DocumentPage:
        self.calls.append(("list_documents", app_id, kw))
        return DocumentPage(
            items=self.documents,
            total=self.total if self.total is not None else len(self.documents),
            page=kw["page"],
            page_size=kw["page_size"],
        )

    async def get_document(self, app_id: UUID, document_id: UUID) -> Document:
        self.calls.append(("get_document", app_id, document_id))
        if self.not_found:
            raise DocumentNotFoundError()
        return self.documents[0]

    async def list_chunks(self, app_id: UUID, document_id: UUID, **kw: Any) -> ChunkPage:
        self.calls.append(("list_chunks", app_id, document_id, kw))
        return ChunkPage(
            items=self.chunks,
            total=self.total if self.total is not None else len(self.chunks),
            page=kw["page"],
            page_size=kw["page_size"],
        )

    # ---------------------------------------------------------------- 写
    def _doc(self, app_id: UUID) -> Document:
        return self.documents[0] if self.documents else _document(app_id)

    async def upload_file(
        self, app_id: UUID, data: bytes, filename: str, *, title: str | None = None
    ) -> UploadResult:
        self.calls.append(("upload_file", app_id, data, filename, title))
        doc = self._doc(app_id)
        job = None if self.duplicate else _job(app_id, doc.id, "ingest")
        return UploadResult(document=doc, duplicate=self.duplicate, job=job)

    async def import_url(self, app_id: UUID, url: str, *, title: str | None = None) -> UploadResult:
        self.calls.append(("import_url", app_id, url, title))
        doc = self._doc(app_id)
        return UploadResult(
            document=doc, duplicate=self.duplicate, job=_job(app_id, doc.id, "ingest")
        )

    async def replace_file(
        self, app_id: UUID, document_id: UUID, data: bytes, filename: str
    ) -> ReplaceResult:
        self.calls.append(("replace_file", app_id, document_id, data, filename))
        if self.busy:
            raise DocumentBusyError()
        doc = self._doc(app_id)
        job = None if self.unchanged else _job(app_id, doc.id, "ingest")
        return ReplaceResult(document=doc, unchanged=self.unchanged, job=job)

    async def rebuild_document(self, app_id: UUID, document_id: UUID) -> Job:
        self.calls.append(("rebuild_document", app_id, document_id))
        if self.busy:
            raise DocumentBusyError()
        return _job(app_id, document_id, "ingest")

    async def update_document(
        self,
        app_id: UUID,
        document_id: UUID,
        *,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
        effective_at: Any = None,
        expires_at: Any = None,
    ) -> Document:
        self.calls.append(
            ("update_document", app_id, document_id, title, metadata, effective_at, expires_at)
        )
        doc = self._doc(app_id)
        if title is not None:
            doc.title = title
        return doc

    async def delete_document(self, app_id: UUID, document_id: UUID) -> Job:
        self.calls.append(("delete_document", app_id, document_id))
        if self.not_found:
            raise DocumentNotFoundError()
        return _job(app_id, document_id, "cleanup_document")


class FakeRuntime:
    def __init__(self, service: FakeService) -> None:
        self.service_ = service

    @asynccontextmanager
    async def session(self) -> AsyncIterator[None]:
        yield None

    def service(self, session: Any) -> FakeService:  # noqa: ARG002 - 与 KnowledgeRuntime 同形
        return self.service_


def _client(
    service: FakeService,
    *,
    scopes: tuple[str, ...] = ("kb:read",),
    idem: Any = None,
) -> tuple[TestClient, FakeAuth]:
    app = create_app()
    auth = FakeAuth(scopes)
    app.state.authenticator = auth
    app.state.knowledge_runtime = FakeRuntime(service)
    if idem is not None:
        app.state.idempotency = idem
    return TestClient(app, raise_server_exceptions=False), auth


def _job(app_id: UUID, target_id: UUID, kind: str) -> Job:
    return Job(id=uuid4(), app_id=app_id, type=kind, target_id=target_id, payload={})


def _document(app_id: UUID, **kw: Any) -> Document:
    fields: dict[str, Any] = {
        "id": uuid4(),
        "app_id": app_id,
        "title": "退货政策.md",
        "source_type": "upload",
        "mime_type": "text/markdown",
        "size_bytes": 1234,
        "status": "done",
        "progress": 100,
        "chunk_count": 2,
        "index_version": 1,
        "tags": ["售后"],
        "created_at": datetime(2026, 10, 17, 12, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 10, 17, 12, 5, tzinfo=UTC),
    }
    fields.update(kw)
    return Document(**fields)


def _chunk(app_id: UUID, document_id: UUID, **kw: Any) -> Chunk:
    fields: dict[str, Any] = {
        "id": uuid4(),
        "app_id": app_id,
        "document_id": document_id,
        "index_version": 1,
        "ordinal": 0,
        "heading_path": "第一章 > 退货",
        "text": "自签收之日起 7 天内可退货。",
        "tokens": 12,
        "page_from": 1,
        "page_to": 1,
        "enabled": True,
        "review_status": "clean",
    }
    fields.update(kw)
    return Chunk(**fields)


def test_document_status_literal_matches_model() -> None:
    assert set(get_args(DocumentStatus)) == set(DOCUMENT_STATUSES)


def test_list_documents_returns_page_body_and_rate_headers() -> None:
    app_id = uuid4()
    doc = _document(app_id)
    service = FakeService(documents=[doc], total=7)
    client, auth = _client(service)

    response = client.get(f"/api/v1/apps/{app_id}/documents?page=2&page_size=5")

    assert response.status_code == 200
    body = response.json()
    assert (body["total"], body["page"], body["page_size"]) == (7, 2, 5)
    assert body["items"][0]["id"] == str(doc.id)
    assert body["items"][0]["status"] == "done"
    assert body["items"][0]["tags"] == ["售后"]
    assert body["items"][0]["created_at"].startswith("2026-10-17T12:00:00")
    assert response.headers["X-RateLimit-Limit"] == "60"
    assert service.calls[0][2] == {"status": None, "q": None, "page": 2, "page_size": 5}
    assert auth.calls[0][1] == ("kb:read",)


def test_list_documents_forwards_filters() -> None:
    app_id = uuid4()
    service = FakeService(documents=[_document(app_id)])
    client, _ = _client(service)

    response = client.get(
        f"/api/v1/apps/{app_id}/documents?status=failed&q=%E9%80%80%E8%B4%A7&page=3"
    )

    assert response.status_code == 200
    assert service.calls[0][2] == {"status": "failed", "q": "退货", "page": 3, "page_size": 20}


def test_get_document_returns_detail() -> None:
    app_id = uuid4()
    doc = _document(app_id)
    service = FakeService(documents=[doc])
    client, _ = _client(service)

    response = client.get(f"/api/v1/apps/{app_id}/documents/{doc.id}")

    assert response.status_code == 200
    assert response.json()["id"] == str(doc.id)
    assert service.calls[0] == ("get_document", app_id, doc.id)


def test_get_document_of_another_app_is_404() -> None:
    app_id = uuid4()
    service = FakeService(documents=[_document(app_id)], not_found=True)
    client, _ = _client(service)

    response = client.get(f"/api/v1/apps/{app_id}/documents/{uuid4()}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"


def test_list_chunks_returns_page_body() -> None:
    app_id, document_id = uuid4(), uuid4()
    chunk = _chunk(
        app_id, document_id, review_status="flagged", flag_reason="疑似指令注入", enabled=False
    )
    service = FakeService(chunks=[chunk], total=42)
    client, _ = _client(service)

    response = client.get(
        f"/api/v1/apps/{app_id}/documents/{document_id}/chunks?page=1&page_size=100"
    )

    assert response.status_code == 200
    body = response.json()
    assert (body["total"], body["page"], body["page_size"]) == (42, 1, 100)
    item = body["items"][0]
    assert item["id"] == str(chunk.id)
    assert (item["ordinal"], item["enabled"], item["review_status"]) == (0, False, "flagged")
    assert item["flag_reason"] == "疑似指令注入"
    assert item["heading_path"] == "第一章 > 退货"


@pytest.mark.parametrize(
    "query",
    ["status=bogus", "page=0", "page_size=0", "page_size=101", f"q={'x' * 101}"],
)
def test_invalid_query_is_422(query: str) -> None:
    app_id = uuid4()
    client, _ = _client(FakeService())

    response = client.get(f"/api/v1/apps/{app_id}/documents?{query}")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize(
    ("page", "page_size", "expected"),
    [(0, 0, (1, 1, 0)), (1, 20, (1, 20, 0)), (3, 5, (3, 5, 10)), (1, 500, (1, PAGE_MAX, 0))],
)
def test_page_args_normalizes(page: int, page_size: int, expected: tuple[int, int, int]) -> None:
    assert page_args(page, page_size) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("退货", "%退货%"), ("50%", "%50\\%%"), ("a_b", "%a\\_b%"), ("a\\b", "%a\\\\b%")],
)
def test_like_pattern_escapes_wildcards(raw: str, expected: str) -> None:
    assert like_pattern(raw) == expected


# ---------------------------------------------------------------------- 写接口
FILE_TEXT = "# 退货\n7 天内可退。".encode()


def _file(name: str = "退货政策.md") -> dict[str, tuple[str, bytes, str]]:
    return {"file": (name, FILE_TEXT, "text/markdown")}


def test_upload_document_accepts_multipart_and_returns_202() -> None:
    app_id = uuid4()
    doc = _document(app_id, status="queued", progress=0)
    service = FakeService(documents=[doc])
    client, auth = _client(service, scopes=("kb:write",))

    response = client.post(
        f"/api/v1/apps/{app_id}/documents",
        files=_file(),
        data={"title": "退货政策"},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["id"] == str(doc.id)
    assert body["status"] == "queued"
    assert body["duplicate"] is False
    assert body["job_id"] is not None
    assert auth.calls[0][1] == ("kb:write",)
    assert service.calls[0][0] == "upload_file"
    assert service.calls[0][2] == FILE_TEXT
    assert service.calls[0][3] == "退货政策.md"
    assert service.calls[0][4] == "退货政策"


def test_upload_duplicate_document_returns_200_without_job() -> None:
    app_id = uuid4()
    service = FakeService(documents=[_document(app_id)], duplicate=True)
    client, _ = _client(service, scopes=("kb:write",))

    response = client.post(f"/api/v1/apps/{app_id}/documents", files=_file())

    assert response.status_code == 200
    assert response.json()["duplicate"] is True
    assert response.json()["job_id"] is None


def test_upload_rejects_unknown_extension_before_reading() -> None:
    app_id = uuid4()
    service = FakeService()
    client, _ = _client(service, scopes=("kb:write",))

    response = client.post(f"/api/v1/apps/{app_id}/documents", files=_file("virus.exe"))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "UNSUPPORTED_FILE_TYPE"
    assert service.calls == []


def test_upload_over_limit_is_413(monkeypatch: pytest.MonkeyPatch) -> None:
    app_id = uuid4()
    client, _ = _client(FakeService(), scopes=("kb:write",))
    monkeypatch.setattr(
        knowledge_router,
        "get_settings",
        lambda: SimpleNamespace(upload_max_mb=0, redis_prefix="aics:"),
    )

    response = client.post(f"/api/v1/apps/{app_id}/documents", files=_file())

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "FILE_TOO_LARGE"
    assert response.json()["error"]["details"]["max_bytes"] == 0


@pytest.mark.parametrize("raw", ["not-json", "[1,2]", '"text"'])
def test_upload_metadata_must_be_a_json_object(raw: str) -> None:
    app_id = uuid4()
    client, _ = _client(FakeService(), scopes=("kb:write",))

    response = client.post(
        f"/api/v1/apps/{app_id}/documents", files=_file(), data={"metadata": raw}
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_upload_is_replayed_for_the_same_idempotency_key() -> None:
    app_id = uuid4()
    service = FakeService(documents=[_document(app_id)])
    redis = FakeRedis()
    client, _ = _client(service, scopes=("kb:write",), idem=Idempotency(redis, prefix="aics:"))
    headers = {"Idempotency-Key": "k-1"}

    first = client.post(f"/api/v1/apps/{app_id}/documents", files=_file(), headers=headers)
    second = client.post(f"/api/v1/apps/{app_id}/documents", files=_file(), headers=headers)

    assert (first.status_code, second.status_code) == (202, 202)
    assert first.json() == second.json()
    assert len([c for c in service.calls if c[0] == "upload_file"]) == 1
    assert "aics:idem:" in next(iter(redis.data))


def test_upload_with_same_key_and_other_body_is_409() -> None:
    app_id = uuid4()
    service = FakeService(documents=[_document(app_id)])
    redis = FakeRedis()
    client, _ = _client(service, scopes=("kb:write",), idem=Idempotency(redis, prefix="aics:"))
    headers = {"Idempotency-Key": "k-2"}

    client.post(f"/api/v1/apps/{app_id}/documents", files=_file(), headers=headers)
    response = client.post(
        f"/api/v1/apps/{app_id}/documents",
        files={"file": ("other.md", "# 别的\n内容。".encode(), "text/markdown")},
        headers=headers,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert len([c for c in service.calls if c[0] == "upload_file"]) == 1


def test_import_url_returns_202() -> None:
    app_id = uuid4()
    service = FakeService(documents=[_document(app_id, source_type="url")])
    client, _ = _client(service, scopes=("kb:write",))

    response = client.post(
        f"/api/v1/apps/{app_id}/documents/url",
        json={"url": "https://example.com/faq.html", "title": "FAQ"},
    )

    assert response.status_code == 202
    assert service.calls[0][:2] == ("import_url", app_id)
    assert service.calls[0][2:] == ("https://example.com/faq.html", "FAQ")


def test_replace_file_unchanged_returns_200() -> None:
    app_id, document_id = uuid4(), uuid4()
    service = FakeService(documents=[_document(app_id, id=document_id)], unchanged=True)
    client, _ = _client(service, scopes=("kb:write",))

    response = client.put(f"/api/v1/apps/{app_id}/documents/{document_id}/file", files=_file())

    assert response.status_code == 200
    assert response.json()["unchanged"] is True
    assert response.json()["job_id"] is None
    assert service.calls[0][2] == document_id


def test_replace_file_busy_is_409() -> None:
    app_id, document_id = uuid4(), uuid4()
    client, _ = _client(FakeService(busy=True), scopes=("kb:write",))

    response = client.put(f"/api/v1/apps/{app_id}/documents/{document_id}/file", files=_file())

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DOCUMENT_BUSY"


def test_reindex_returns_202_with_job() -> None:
    app_id, document_id = uuid4(), uuid4()
    service = FakeService()
    client, _ = _client(service, scopes=("kb:write",))

    response = client.post(f"/api/v1/apps/{app_id}/documents/{document_id}/reindex")

    assert response.status_code == 202
    body = response.json()
    assert body["id"] == str(document_id)
    assert body["job_id"]
    assert service.calls[0] == ("rebuild_document", app_id, document_id)


def test_patch_document_updates_title_and_metadata() -> None:
    app_id, document_id = uuid4(), uuid4()
    service = FakeService(documents=[_document(app_id, id=document_id)])
    client, _ = _client(service, scopes=("kb:write",))

    response = client.patch(
        f"/api/v1/apps/{app_id}/documents/{document_id}",
        json={"title": "退货政策 v2", "metadata": {"owner": "售后"}},
    )

    assert response.status_code == 200
    assert response.json()["title"] == "退货政策 v2"
    assert service.calls[0][:5] == (
        "update_document",
        app_id,
        document_id,
        "退货政策 v2",
        {"owner": "售后"},
    )
    assert service.calls[0][5] is KEEP and service.calls[0][6] is KEEP


def test_patch_document_without_fields_is_422() -> None:
    app_id, document_id = uuid4(), uuid4()
    client, _ = _client(FakeService(), scopes=("kb:write",))

    response = client.patch(f"/api/v1/apps/{app_id}/documents/{document_id}", json={})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_delete_document_returns_204_without_body() -> None:
    app_id, document_id = uuid4(), uuid4()
    service = FakeService()
    client, _ = _client(service, scopes=("kb:write",))

    response = client.delete(f"/api/v1/apps/{app_id}/documents/{document_id}")

    assert response.status_code == 204
    assert response.content == b""
    assert service.calls[0] == ("delete_document", app_id, document_id)


def test_delete_document_of_another_app_is_404() -> None:
    app_id, document_id = uuid4(), uuid4()
    client, _ = _client(FakeService(not_found=True), scopes=("kb:write",))

    response = client.delete(f"/api/v1/apps/{app_id}/documents/{document_id}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"


def test_write_route_needs_kb_write_scope() -> None:
    app_id = uuid4()
    service = FakeService()
    client, _ = _client(service, scopes=("kb:read",))

    response = client.post(f"/api/v1/apps/{app_id}/documents", files=_file())

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"
    assert service.calls == []


def test_runtime_wires_service_dependencies() -> None:
    """回归：`KnowledgeRuntime.service()` 必须把 storage / vector 接到 `KnowledgeService` 的正确形参。

    曾经写成 `store=self.store`（把文件存储当 Qdrant 传，且漏了必填的 `storage`），
    单测用 fake 运行时、集成测试又跑不到，所以专门钉一条：真运行时 + 假依赖也要能构造出 service。
    """
    storage, vector = object(), object()
    runtime = KnowledgeRuntime(sessions=None, storage=storage, vector=vector)  # type: ignore[arg-type]

    service = runtime.service(object())  # type: ignore[arg-type]

    assert service.storage is storage
    assert service.store is vector


def test_runtime_falls_back_to_the_configured_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    """没显式给 storage 时用 `aics.storage.get_storage()`（CLI / 懒加载路径一致）。"""
    configured = object()
    monkeypatch.setattr(knowledge_runtime, "get_storage", lambda: configured)

    service = KnowledgeRuntime(sessions=None).service(object())  # type: ignore[arg-type]

    assert service.storage is configured
    assert service.store is None
