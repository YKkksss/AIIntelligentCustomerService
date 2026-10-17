"""M3.2 ②：知识库开放接口（读）—— 路由参数、序列化与错误码（开放接口参考 §4 / 接口规范 §5）。

用 fake 认证器 + fake 运行时跑真路由（`create_app()` 挂载），不需要数据库；
数据库行为由 `tests/integration/test_isolation_pg.py` 与 `test_knowledge_*.py` 覆盖。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, get_args
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from aics.core.errors import DocumentNotFoundError
from aics.core.security import Principal
from aics.db.models import Chunk, Document
from aics.db.models.knowledge import DOCUMENT_STATUSES
from aics.ingest.repository import like_pattern
from aics.main import create_app
from aics.modules.knowledge.router import DocumentStatus
from aics.modules.knowledge.service import PAGE_MAX, ChunkPage, DocumentPage, page_args


class FakeAuth:
    """只校验 scope 是否被请求；真实校验由 tests/unit/test_apikeys.py 覆盖。"""

    def __init__(self, scopes: tuple[str, ...] = ("kb:read",)) -> None:
        self.scopes = scopes
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
        return Principal(
            app_id=app_id,
            key_id=uuid4(),
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
    ) -> None:
        self.documents = list(documents or [])
        self.chunks = list(chunks or [])
        self.total = total
        self.not_found = not_found
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


class FakeRuntime:
    def __init__(self, service: FakeService) -> None:
        self.service_ = service

    @asynccontextmanager
    async def session(self) -> AsyncIterator[None]:
        yield None

    def service(self, session: Any) -> FakeService:  # noqa: ARG002 - 与 KnowledgeRuntime 同形
        return self.service_


def _client(
    service: FakeService, *, scopes: tuple[str, ...] = ("kb:read",)
) -> tuple[TestClient, FakeAuth]:
    app = create_app()
    auth = FakeAuth(scopes)
    app.state.authenticator = auth
    app.state.knowledge_runtime = FakeRuntime(service)
    return TestClient(app, raise_server_exceptions=False), auth


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
