"""M4.3 ②：`/api/v1/admin/apps/{app_id}/...` 知识库管理接口的协议层。

业务实现（切块、任务、审计）由集成用例覆盖；这里只证明 router 把参数、`created_by`、
`chunk_config` 正确地交给了 service，并把结果按约定序列化。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from aics.core.errors import DocumentNotFoundError, ForbiddenError, UnauthenticatedError
from aics.core.security import AuthedUser
from aics.db.models import Chunk, Document, Job
from aics.main import create_app
from aics.modules.knowledge.service import (
    AppChunkPage,
    ChunkPage,
    DocumentPage,
    ReplaceResult,
    UploadResult,
)

APP_ID = uuid4()
DOC_ID = uuid4()
CHUNK_ID = uuid4()


def _document(**kw: Any) -> Document:
    fields: dict[str, Any] = {
        "id": DOC_ID,
        "app_id": APP_ID,
        "title": "退货政策.md",
        "source_type": "file",
        "mime_type": "text/markdown",
        "size_bytes": 1234,
        "status": "queued",
        "progress": 0,
        "chunk_count": 2,
        "index_version": 1,
        "tags": ["售后"],
        "created_at": datetime(2026, 10, 20, 12, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 10, 20, 12, 5, tzinfo=UTC),
    }
    fields.update(kw)
    return Document(**fields)


def _chunk() -> Chunk:
    return Chunk(
        id=uuid4(),
        app_id=APP_ID,
        document_id=DOC_ID,
        index_version=1,
        ordinal=0,
        heading_path="第一章 > 退货",
        text="自签收之日起 7 天内可退货。",
        tokens=12,
        page_from=1,
        page_to=1,
        enabled=True,
        review_status="none",
    )


def _job(kind: str = "ingest") -> Job:
    return Job(
        id=uuid4(),
        app_id=APP_ID,
        type=kind,
        status="queued",
        progress=0,
        target_id=DOC_ID,
        payload={},
        created_at=datetime(2026, 10, 20, 12, 0, tzinfo=UTC),
    )


class FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.error: Exception | None = None

    def _maybe_raise(self) -> None:
        if self.error is not None:
            raise self.error

    async def list_documents(self, app_id: Any, **kw: Any) -> DocumentPage:
        self.calls.append(("list_documents", app_id, kw))
        self._maybe_raise()
        return DocumentPage(
            items=[_document()], total=1, page=kw["page"], page_size=kw["page_size"]
        )

    async def get_document(self, app_id: Any, document_id: Any) -> Document:
        self.calls.append(("get_document", app_id, document_id))
        self._maybe_raise()
        return _document()

    async def list_chunks(self, app_id: Any, document_id: Any, **kw: Any) -> ChunkPage:
        self.calls.append(("list_chunks", app_id, document_id, kw))
        return ChunkPage(items=[_chunk()], total=1, page=kw["page"], page_size=kw["page_size"])

    async def list_jobs(self, app_id: Any, **kw: Any) -> tuple[list[Job], int]:
        self.calls.append(("list_jobs", app_id, kw))
        return [_job()], 1

    async def upload_file(self, app_id: Any, data: bytes, filename: str, **kw: Any) -> UploadResult:
        self.calls.append(("upload_file", app_id, data, filename, kw))
        return UploadResult(document=_document(), duplicate=False, job=_job())

    async def import_url(self, app_id: Any, url: str, **kw: Any) -> UploadResult:
        self.calls.append(("import_url", app_id, url, kw))
        return UploadResult(document=_document(), duplicate=True, job=None)

    async def replace_file(
        self, app_id: Any, document_id: Any, data: bytes, filename: str, **kw: Any
    ) -> ReplaceResult:
        self.calls.append(("replace_file", app_id, document_id, data, filename, kw))
        return ReplaceResult(document=_document(), unchanged=False, job=_job("replace"))

    async def rebuild_document(self, app_id: Any, document_id: Any, **kw: Any) -> Job:
        self.calls.append(("rebuild_document", app_id, document_id, kw))
        return _job("reindex_document")

    async def update_document(self, app_id: Any, document_id: Any, **kw: Any) -> Document:
        self.calls.append(("update_document", app_id, document_id, kw))
        return _document(title=kw.get("title") or "退货政策.md")

    async def delete_document(self, app_id: Any, document_id: Any, **kw: Any) -> Job:
        self.calls.append(("delete_document", app_id, document_id, kw))
        return _job("cleanup_document")

    async def preview_upload(self, app_id: Any, data: bytes, filename: str, **kw: Any) -> dict:
        self.calls.append(("preview_upload", app_id, data, filename, kw))
        return {"total_chunks": 3, "total_parents": 1, "total_tokens": 90, "chunks": []}

    async def preview_document(self, app_id: Any, document_id: Any, **kw: Any) -> dict:
        self.calls.append(("preview_document", app_id, document_id, kw))
        return {"total_chunks": 2, "total_parents": 1, "total_tokens": 60, "chunks": []}

    async def hit_test(self, app_id: Any, query: str, **kw: Any) -> dict:
        self.calls.append(("hit_test", app_id, query, kw))
        return {"query": query, "fused": [], "rerank_applied": True}

    async def list_app_chunks(self, app_id: Any, **kw: Any) -> dict:
        self.calls.append(("list_app_chunks", app_id, kw))
        return AppChunkPage(
            items=[
                {
                    "id": CHUNK_ID,
                    "document_id": DOC_ID,
                    "document_title": "退货政策.md",
                    "ordinal": 0,
                    "heading_path": "第一章",
                    "text": "自签收之日起 7 天内可退货。",
                    "tokens": 12,
                    "enabled": True,
                    "review_status": "flagged",
                    "flag_reason": "疑似恶意指令",
                    "reviewed_at": None,
                }
            ],
            total=1,
            page=kw["page"],
            page_size=kw["page_size"],
        )

    async def update_chunk(self, app_id: Any, chunk_id: Any, **kw: Any) -> Chunk:
        self.calls.append(("update_chunk", app_id, chunk_id, kw))
        self._maybe_raise()
        chunk = _chunk()
        chunk.id = chunk_id
        if kw.get("enabled") is not None:
            chunk.enabled = kw["enabled"]
        if kw.get("review_status") is not None:
            chunk.review_status = kw["review_status"]
            chunk.reviewed_at = datetime(2026, 10, 20, 12, 30, tzinfo=UTC)
        return chunk


class FakeRuntime:
    def __init__(self, service: FakeService, *, reranker: Any = None) -> None:
        self._service = service
        self.reranker = reranker if reranker is not None else object()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[None]:
        yield None

    def service(self, _session: Any) -> FakeService:
        return self._service


class FakeAuthorizer:
    def __init__(self, deny: str | None = None) -> None:
        self.deny = deny
        self.user_id = uuid4()

    async def authenticate(self, authorization: str | None) -> AuthedUser:
        if authorization != "Bearer good-token":
            raise UnauthenticatedError(details={"reason": "missing_token"})
        return AuthedUser(
            id=self.user_id, email="admin@example.com", display_name="管理员", is_super_admin=False
        )

    async def authorize(self, user: Any, *, app_id: Any, permission: str) -> None:
        if self.deny is not None and permission == self.deny:
            raise ForbiddenError(details={"reason": "missing_permission", "permission": permission})


@pytest.fixture
def client() -> tuple[TestClient, FakeService, FakeAuthorizer]:
    service = FakeService()
    authorizer = FakeAuthorizer()
    app = create_app()
    app.state.knowledge_runtime = FakeRuntime(service)
    app.state.authorizer = authorizer
    return TestClient(app), service, authorizer


@pytest.fixture
def headers() -> dict[str, str]:
    return {"authorization": "Bearer good-token"}


def test_list_documents_forwards_filters_and_page(
    client: tuple[TestClient, FakeService, FakeAuthorizer], headers: dict[str, str]
) -> None:
    http, service, _ = client

    resp = http.get(
        f"/api/v1/admin/apps/{APP_ID}/documents?status=done&q=退货&page=2&page_size=10",
        headers=headers,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1 and body["page"] == 2
    assert body["items"][0]["title"] == "退货政策.md"
    assert body["items"][0]["created_at"].startswith("2026-10-20")
    kind, app_id, kwargs = service.calls[0]
    assert (kind, app_id) == ("list_documents", APP_ID)
    assert kwargs == {"status": "done", "q": "退货", "page": 2, "page_size": 10}


def test_detail_maps_not_found(
    client: tuple[TestClient, FakeService, FakeAuthorizer], headers: dict[str, str]
) -> None:
    http, service, _ = client
    service.error = DocumentNotFoundError()

    resp = http.get(f"/api/v1/admin/apps/{APP_ID}/documents/{DOC_ID}", headers=headers)

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"


def test_chunks_are_serialised_with_review_fields(
    client: tuple[TestClient, FakeService, FakeAuthorizer], headers: dict[str, str]
) -> None:
    http, _service, _ = client

    resp = http.get(
        f"/api/v1/admin/apps/{APP_ID}/documents/{DOC_ID}/chunks?page_size=5", headers=headers
    )

    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["review_status"] == "none" and item["enabled"] is True


def test_jobs_encode_datetimes(
    client: tuple[TestClient, FakeService, FakeAuthorizer], headers: dict[str, str]
) -> None:
    http, service, _ = client

    resp = http.get(f"/api/v1/admin/apps/{APP_ID}/jobs?type=ingest", headers=headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["type"] == "ingest"
    assert body["items"][0]["created_at"].startswith("2026-10-20")
    assert service.calls[0][2] == {"type": "ingest", "status": None, "page": 1, "page_size": 20}


def test_upload_passes_actor_and_chunk_config(
    client: tuple[TestClient, FakeService, FakeAuthorizer], headers: dict[str, str]
) -> None:
    http, service, authorizer = client

    resp = http.post(
        f"/api/v1/admin/apps/{APP_ID}/documents",
        headers=headers,
        files={"file": ("policy.md", b"# policy", "text/markdown")},
        data={"title": "退货政策", "chunk_config": '{"chunk_size": 400}'},
    )

    assert resp.status_code == 202
    assert resp.json()["status"] == "queued"
    kind, _app_id, _data, filename, kwargs = service.calls[0]
    assert (kind, filename) == ("upload_file", "policy.md")
    assert kwargs["created_by"] == authorizer.user_id
    assert kwargs["chunk_config"] == {"chunk_size": 400}
    assert kwargs["title"] == "退货政策"


def test_upload_requires_kb_write(
    client: tuple[TestClient, FakeService, FakeAuthorizer], headers: dict[str, str]
) -> None:
    http, _service, authorizer = client
    authorizer.deny = "kb.write"

    resp = http.post(
        f"/api/v1/admin/apps/{APP_ID}/documents",
        headers=headers,
        files={"file": ("policy.md", b"# policy", "text/markdown")},
    )

    assert resp.status_code == 403


def test_upload_rejects_bad_chunk_config_json(
    client: tuple[TestClient, FakeService, FakeAuthorizer], headers: dict[str, str]
) -> None:
    http, _service, _ = client

    resp = http.post(
        f"/api/v1/admin/apps/{APP_ID}/documents",
        headers=headers,
        files={"file": ("policy.md", b"# policy", "text/markdown")},
        data={"chunk_config": "{oops"},
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["errors"][0]["loc"] == ["chunk_config"]


def test_preview_upload_returns_chunk_totals(
    client: tuple[TestClient, FakeService, FakeAuthorizer], headers: dict[str, str]
) -> None:
    http, service, _ = client

    resp = http.post(
        f"/api/v1/admin/apps/{APP_ID}/documents/preview?limit=20",
        headers=headers,
        files={"file": ("policy.md", b"# policy", "text/markdown")},
        data={"chunk_config": '{"preset": "faq"}'},
    )

    assert resp.status_code == 200
    assert resp.json()["total_chunks"] == 3
    kwargs = service.calls[0][-1]
    assert kwargs["chunk_config"] == {"preset": "faq"} and kwargs["limit"] == 20


def test_reindex_passes_config_and_reset(
    client: tuple[TestClient, FakeService, FakeAuthorizer], headers: dict[str, str]
) -> None:
    http, service, authorizer = client

    resp = http.post(
        f"/api/v1/admin/apps/{APP_ID}/documents/{DOC_ID}/reindex",
        headers=headers,
        json={"chunk_config": {"overlap": 40}},
    )

    assert resp.status_code == 202
    kwargs = service.calls[0][-1]
    assert kwargs["chunk_config"] == {"overlap": 40}
    assert kwargs["reset_chunk_config"] is False
    assert kwargs["created_by"] == authorizer.user_id


def test_patch_document_passes_title_and_metadata(
    client: tuple[TestClient, FakeService, FakeAuthorizer], headers: dict[str, str]
) -> None:
    http, service, authorizer = client

    resp = http.patch(
        f"/api/v1/admin/apps/{APP_ID}/documents/{DOC_ID}",
        headers=headers,
        json={"title": "新标题", "metadata": {"owner": "售后"}},
    )

    assert resp.status_code == 200
    kwargs = service.calls[0][-1]
    assert kwargs["title"] == "新标题"
    assert kwargs["metadata"] == {"owner": "售后"}
    assert kwargs["created_by"] == authorizer.user_id


def test_delete_returns_202_with_job(
    client: tuple[TestClient, FakeService, FakeAuthorizer], headers: dict[str, str]
) -> None:
    http, service, authorizer = client

    resp = http.delete(f"/api/v1/admin/apps/{APP_ID}/documents/{DOC_ID}", headers=headers)

    assert resp.status_code == 202
    assert resp.json()["job_id"]
    assert service.calls[0][-1]["created_by"] == authorizer.user_id


def test_hit_test_rejects_blank_query(
    client: tuple[TestClient, FakeService, FakeAuthorizer], headers: dict[str, str]
) -> None:
    http, _service, _ = client

    resp = http.post(
        f"/api/v1/admin/apps/{APP_ID}/hit-test", headers=headers, json={"query": "   "}
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["errors"][0]["loc"] == ["query"]


def test_hit_test_forwards_reranker_and_overrides(
    client: tuple[TestClient, FakeService, FakeAuthorizer], headers: dict[str, str]
) -> None:
    http, service, _ = client

    resp = http.post(
        f"/api/v1/admin/apps/{APP_ID}/hit-test",
        headers=headers,
        json={"query": "多久能退货", "overrides": {"top_k": 3}, "filters": {"tags": ["售后"]}},
    )

    assert resp.status_code == 200
    kind, _app_id, query, kwargs = service.calls[0]
    assert (kind, query) == ("hit_test", "多久能退货")
    assert kwargs["overrides"] == {"top_k": 3}
    assert kwargs["filters"] == {"tags": ["售后"]}
    assert kwargs["reranker"] is not None


def test_missing_token_is_401(
    client: tuple[TestClient, FakeService, FakeAuthorizer],
) -> None:
    http, _service, _ = client

    resp = http.get(f"/api/v1/admin/apps/{APP_ID}/documents")

    assert resp.status_code == 401


# ---------------------------------------------------------------------- 切块复核（M4.3 ③）
def test_list_app_chunks_forwards_filters(
    client: tuple[TestClient, FakeService, FakeAuthorizer], headers: dict[str, str]
) -> None:
    http, service, _ = client

    resp = http.get(
        f"/api/v1/admin/apps/{APP_ID}/chunks"
        f"?review_status=flagged&document_id={DOC_ID}&enabled=true&page=2&page_size=5",
        headers=headers,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1 and body["page"] == 2
    item = body["items"][0]
    assert item["document_title"] == "退货政策.md"
    assert item["review_status"] == "flagged" and item["flag_reason"] == "疑似恶意指令"
    kind, app_id, kwargs = service.calls[0]
    assert (kind, app_id) == ("list_app_chunks", APP_ID)
    assert kwargs == {
        "document_id": DOC_ID,
        "review_status": "flagged",
        "enabled": True,
        "page": 2,
        "page_size": 5,
    }


def test_list_app_chunks_rejects_bad_review_status(
    client: tuple[TestClient, FakeService, FakeAuthorizer], headers: dict[str, str]
) -> None:
    http, service, _ = client

    resp = http.get(f"/api/v1/admin/apps/{APP_ID}/chunks?review_status=bogus", headers=headers)

    assert resp.status_code == 422
    assert service.calls == []


def test_patch_chunk_disable_forwards_operator(
    client: tuple[TestClient, FakeService, FakeAuthorizer], headers: dict[str, str]
) -> None:
    http, service, authorizer = client

    resp = http.patch(
        f"/api/v1/admin/apps/{APP_ID}/chunks/{CHUNK_ID}", headers=headers, json={"enabled": False}
    )

    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
    kind, app_id, chunk_id, kwargs = service.calls[0]
    assert (kind, app_id, chunk_id) == ("update_chunk", APP_ID, CHUNK_ID)
    assert kwargs["enabled"] is False and kwargs["review_status"] is None
    assert kwargs["created_by"] == authorizer.user_id


def test_patch_chunk_approve_returns_reviewed_at(
    client: tuple[TestClient, FakeService, FakeAuthorizer], headers: dict[str, str]
) -> None:
    http, service, _ = client

    resp = http.patch(
        f"/api/v1/admin/apps/{APP_ID}/chunks/{CHUNK_ID}",
        headers=headers,
        json={"review_status": "approved"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["review_status"] == "approved"
    assert body["reviewed_at"].startswith("2026-10-20")
    _kind, _app_id, _chunk_id, kwargs = service.calls[0]
    assert kwargs["review_status"] == "approved" and kwargs["enabled"] is None


def test_patch_chunk_requires_a_field(
    client: tuple[TestClient, FakeService, FakeAuthorizer], headers: dict[str, str]
) -> None:
    http, service, _ = client

    resp = http.patch(f"/api/v1/admin/apps/{APP_ID}/chunks/{CHUNK_ID}", headers=headers, json={})

    assert resp.status_code == 422
    assert service.calls == []


def test_patch_chunk_rejects_unapproved_review_status(
    client: tuple[TestClient, FakeService, FakeAuthorizer], headers: dict[str, str]
) -> None:
    http, service, _ = client

    resp = http.patch(
        f"/api/v1/admin/apps/{APP_ID}/chunks/{CHUNK_ID}",
        headers=headers,
        json={"review_status": "flagged"},
    )

    assert resp.status_code == 422
    assert service.calls == []


def test_chunk_patch_needs_write_permission(
    client: tuple[TestClient, FakeService, FakeAuthorizer], headers: dict[str, str]
) -> None:
    http, _service, authorizer = client
    authorizer.deny = "kb.write"

    resp = http.patch(
        f"/api/v1/admin/apps/{APP_ID}/chunks/{CHUNK_ID}", headers=headers, json={"enabled": False}
    )

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"
