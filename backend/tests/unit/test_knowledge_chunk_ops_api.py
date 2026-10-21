"""M4.3b ③：切块级运营的协议层（新增 / 删除 / 近重复 / 补充问题 / 文档有效期）。

业务规则见 `test_knowledge_chunk_ops.py`；这里证明 router 把参数与 `created_by` 交对了、
序列化与状态码符合约定、只传部分有效期字段时另一侧保持哨兵值。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from aics.core.errors import ForbiddenError, UnauthenticatedError
from aics.core.security import AuthedUser
from aics.db.models import Chunk, Document
from aics.main import create_app
from aics.modules.knowledge.service import KEEP

APP_ID = uuid4()
DOC_ID = uuid4()
CHUNK_ID = uuid4()
QUESTION_ID = uuid4()


class FakeQuestion:
    def __init__(self) -> None:
        self.id = QUESTION_ID
        self.chunk_id = CHUNK_ID
        self.question = "退款要多久到账？"
        self.source = "manual"
        self.status = "active"
        self.created_at = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)


def _chunk(**kw: Any) -> Chunk:
    fields: dict[str, Any] = {
        "id": CHUNK_ID,
        "app_id": APP_ID,
        "document_id": DOC_ID,
        "index_version": 1,
        "ordinal": 3,
        "heading_path": "第一章",
        "text": "加急订单 24 小时内发货。",
        "tokens": 9,
        "enabled": True,
        "review_status": "none",
        "source": "manual",
    }
    fields.update(kw)
    return Chunk(**fields)


def _document() -> Document:
    return Document(
        id=DOC_ID,
        app_id=APP_ID,
        title="退货政策.md",
        source_type="file",
        mime_type="text/markdown",
        size_bytes=10,
        status="done",
        progress=100,
        chunk_count=1,
        index_version=1,
        tags=[],
        created_at=datetime(2026, 10, 21, 9, 0, tzinfo=UTC),
        updated_at=datetime(2026, 10, 21, 9, 0, tzinfo=UTC),
    )


class FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def add_chunk(self, app_id: Any, document_id: Any, **kw: Any) -> Chunk:
        self.calls.append(("add_chunk", app_id, document_id, kw))
        return _chunk(text=kw["text"], heading_path=kw.get("heading_path"))

    async def delete_chunk(self, app_id: Any, chunk_id: Any, **kw: Any) -> None:
        self.calls.append(("delete_chunk", app_id, chunk_id, kw))

    async def duplicates(self, app_id: Any, document_id: Any) -> list[dict[str, Any]]:
        self.calls.append(("duplicates", app_id, document_id))
        return [{"document_id": str(uuid4()), "title": "退货政策（副本）.md", "distance": 1}]

    async def list_chunk_questions(self, app_id: Any, chunk_id: Any) -> list[FakeQuestion]:
        self.calls.append(("list_chunk_questions", app_id, chunk_id))
        return [FakeQuestion()]

    async def add_chunk_questions(
        self, app_id: Any, chunk_id: Any, questions: list[str], **kw: Any
    ) -> list[FakeQuestion]:
        self.calls.append(("add_chunk_questions", app_id, chunk_id, questions, kw))
        return [FakeQuestion(), FakeQuestion()]

    async def delete_chunk_question(self, app_id: Any, question_id: Any) -> None:
        self.calls.append(("delete_chunk_question", app_id, question_id))

    async def reattach_chunk_question(
        self, app_id: Any, question_id: Any, chunk_id: Any
    ) -> FakeQuestion:
        self.calls.append(("reattach_chunk_question", app_id, question_id, chunk_id))
        return FakeQuestion()

    async def update_document(self, app_id: Any, document_id: Any, **kw: Any) -> Document:
        self.calls.append(("update_document", app_id, document_id, kw))
        return _document()


class FakeRuntime:
    def __init__(self, service: FakeService) -> None:
        self._service = service

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


def test_create_chunk_returns_201_and_forwards_actor(
    client: tuple[TestClient, FakeService, FakeAuthorizer], headers: dict[str, str]
) -> None:
    http, service, _ = client

    resp = http.post(
        f"/api/v1/admin/apps/{APP_ID}/documents/{DOC_ID}/chunks",
        json={"text": "加急订单 24 小时内发货。", "heading_path": "第一章"},
        headers=headers,
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["source"] == "manual" and body["ordinal"] == 3
    kind, app_id, document_id, kwargs = service.calls[0]
    assert (kind, app_id, document_id) == ("add_chunk", APP_ID, DOC_ID)
    assert kwargs["text"] == "加急订单 24 小时内发货。"
    assert isinstance(kwargs["created_by"], UUID)


def test_create_chunk_rejects_blank_text(
    client: tuple[TestClient, FakeService, FakeAuthorizer], headers: dict[str, str]
) -> None:
    http, service, _ = client

    resp = http.post(
        f"/api/v1/admin/apps/{APP_ID}/documents/{DOC_ID}/chunks",
        json={"text": ""},
        headers=headers,
    )

    assert resp.status_code == 422
    assert service.calls == []


def test_delete_chunk_returns_204(
    client: tuple[TestClient, FakeService, FakeAuthorizer], headers: dict[str, str]
) -> None:
    http, service, _ = client

    resp = http.delete(f"/api/v1/admin/apps/{APP_ID}/chunks/{CHUNK_ID}", headers=headers)

    assert resp.status_code == 204 and resp.content == b""
    assert service.calls[0][0] == "delete_chunk"
    assert service.calls[0][3]["created_by"] is not None


def test_duplicates_lists_near_duplicates(
    client: tuple[TestClient, FakeService, FakeAuthorizer], headers: dict[str, str]
) -> None:
    http, service, _ = client

    resp = http.get(f"/api/v1/admin/apps/{APP_ID}/documents/{DOC_ID}/duplicates", headers=headers)

    assert resp.status_code == 200
    assert resp.json()["items"][0]["distance"] == 1
    assert service.calls == [("duplicates", APP_ID, DOC_ID)]


def test_questions_list_and_add(
    client: tuple[TestClient, FakeService, FakeAuthorizer], headers: dict[str, str]
) -> None:
    http, service, _ = client

    listed = http.get(f"/api/v1/admin/apps/{APP_ID}/chunks/{CHUNK_ID}/questions", headers=headers)
    created = http.post(
        f"/api/v1/admin/apps/{APP_ID}/chunks/{CHUNK_ID}/questions",
        json={"questions": ["退款要多久到账？", "多久能退？"]},
        headers=headers,
    )

    assert listed.status_code == 200
    assert listed.json()["items"][0]["question"] == "退款要多久到账？"
    assert created.status_code == 201
    assert len(created.json()["items"]) == 2
    kind, _app_id, _chunk_id, questions, kwargs = service.calls[1]
    assert kind == "add_chunk_questions" and questions == ["退款要多久到账？", "多久能退？"]
    assert kwargs["source"] == "manual"


def test_question_delete_and_reattach(
    client: tuple[TestClient, FakeService, FakeAuthorizer], headers: dict[str, str]
) -> None:
    http, service, _ = client
    other = uuid4()

    removed = http.delete(f"/api/v1/admin/apps/{APP_ID}/questions/{QUESTION_ID}", headers=headers)
    moved = http.post(
        f"/api/v1/admin/apps/{APP_ID}/questions/{QUESTION_ID}/reattach",
        json={"chunk_id": str(other)},
        headers=headers,
    )

    assert removed.status_code == 204
    assert moved.status_code == 200 and moved.json()["id"] == str(QUESTION_ID)
    assert service.calls[1] == ("reattach_chunk_question", APP_ID, QUESTION_ID, other)


def test_document_patch_forwards_only_given_validity_fields(
    client: tuple[TestClient, FakeService, FakeAuthorizer], headers: dict[str, str]
) -> None:
    http, service, _ = client

    resp = http.patch(
        f"/api/v1/admin/apps/{APP_ID}/documents/{DOC_ID}",
        json={"expires_at": "2026-12-31T00:00:00Z"},
        headers=headers,
    )

    assert resp.status_code == 200
    _kind, _app_id, _doc_id, kwargs = service.calls[0]
    assert kwargs["expires_at"] == datetime(2026, 12, 31, tzinfo=UTC)
    assert kwargs["effective_at"] is KEEP  # 没传就保持原值


def test_document_patch_without_any_field_is_422(
    client: tuple[TestClient, FakeService, FakeAuthorizer], headers: dict[str, str]
) -> None:
    http, service, _ = client

    resp = http.patch(f"/api/v1/admin/apps/{APP_ID}/documents/{DOC_ID}", json={}, headers=headers)

    assert resp.status_code == 422
    assert service.calls == []


def test_write_requires_kb_write_scope(
    client: tuple[TestClient, FakeService, FakeAuthorizer], headers: dict[str, str]
) -> None:
    http, _service, authorizer = client
    authorizer.deny = "kb.write"

    resp = http.delete(f"/api/v1/admin/apps/{APP_ID}/chunks/{CHUNK_ID}", headers=headers)

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"
