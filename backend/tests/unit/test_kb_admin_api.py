"""M4.7 ③：公共知识库管理接口的协议层。

业务（名称冲突、默认库、关联校验）由集成用例覆盖；这里只验路由把参数、权限与 `actor_id`
正确交给 service，并按响应模型序列化。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from aics.core.errors import ConflictError, ForbiddenError, NotFoundError, UnauthenticatedError
from aics.core.security import AuthedUser
from aics.main import create_app

APP_ID = uuid4()
KB_ID = uuid4()
OTHER_ID = uuid4()


def _row(kb_id: str = str(KB_ID), **kw: Any) -> dict[str, Any]:
    row = {
        "id": kb_id,
        "name": "员工制度库",
        "description": "员工手册与制度",
        "status": "active",
        "chunk_config": {"chunk_size": 400},
        "config_version": 1,
        "document_count": 2,
        "chunk_count": 20,
        "apps": [{"app_id": str(APP_ID), "name": "售后客服"}],
        "created_at": "2026-10-22T10:00:00Z",
        "updated_at": "2026-10-22T10:05:00Z",
    }
    row.update(kw)
    return row


class FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.error: Exception | None = None

    def _raise(self) -> None:
        if self.error is not None:
            raise self.error

    async def list(self, **kw: Any) -> tuple[list[dict[str, Any]], int]:
        self.calls.append(("list", kw))
        self._raise()
        return [_row()], 7

    async def detail(self, kb_id: Any) -> dict[str, Any]:
        self.calls.append(("detail", kb_id))
        self._raise()
        return _row(str(kb_id))

    async def create(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("create", kw))
        self._raise()
        return _row(name=kw["name"])

    async def update(self, kb_id: Any, patch: Any, **kw: Any) -> dict[str, Any]:
        self.calls.append(("update", kb_id, patch, kw))
        self._raise()
        return _row(str(kb_id), name=patch.get("name", "员工制度库"))

    async def delete(self, kb_id: Any, **kw: Any) -> None:
        self.calls.append(("delete", kb_id, kw))
        self._raise()

    async def links_of(self, app_id: Any) -> list[dict[str, Any]]:
        self.calls.append(("links_of", app_id))
        self._raise()
        return [_row(linked=True)]

    async def set_links(self, app_id: Any, kb_ids: Any, **kw: Any) -> list[dict[str, Any]]:
        self.calls.append(("set_links", app_id, kb_ids, kw))
        self._raise()
        return [_row(linked=True)]


class FakeRuntime:
    def __init__(self, service: FakeService) -> None:
        self._service = service

    @asynccontextmanager
    async def session(self) -> AsyncIterator[None]:
        yield None

    def kb_service(self, session: Any) -> FakeService:
        return self._service


class FakeAuthorizer:
    def __init__(self, deny: str | None = None) -> None:
        self.deny = deny
        self.user_id = uuid4()

    async def authenticate(self, authorization: str | None) -> AuthedUser:
        if authorization != "Bearer good-token":
            raise UnauthenticatedError(details={"reason": "missing_token"})
        return AuthedUser(
            id=self.user_id,
            email="kb@example.com",
            display_name="知识库管理员",
            is_super_admin=False,
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


def test_list_forwards_filters_and_returns_totals(
    client: tuple[TestClient, FakeService, FakeAuthorizer], headers: dict[str, str]
) -> None:
    http, service, _ = client

    resp = http.get(
        "/api/v1/admin/knowledge-bases?status=active&q=制度&page=2&page_size=10", headers=headers
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 7 and body["page"] == 2 and body["page_size"] == 10
    assert body["items"][0]["name"] == "员工制度库"
    assert body["items"][0]["apps"][0]["name"] == "售后客服"
    kind, kwargs = service.calls[0]
    assert kind == "list"
    assert kwargs == {"status": "active", "q": "制度", "page": 2, "page_size": 10}


def test_list_rejects_bad_status(
    client: tuple[TestClient, FakeService, FakeAuthorizer], headers: dict[str, str]
) -> None:
    http, _service, _ = client

    resp = http.get("/api/v1/admin/knowledge-bases?status=archived", headers=headers)

    assert resp.status_code == 422


def test_create_returns_201_and_forwards_actor(
    client: tuple[TestClient, FakeService, FakeAuthorizer], headers: dict[str, str]
) -> None:
    http, service, authorizer = client

    resp = http.post(
        "/api/v1/admin/knowledge-bases",
        json={"name": "产品库", "description": "", "chunk_config": {"chunk_size": 400}},
        headers=headers,
    )

    assert resp.status_code == 201
    assert resp.json()["name"] == "产品库"
    kind, kwargs = service.calls[0]
    assert kind == "create"
    assert kwargs["name"] == "产品库"
    assert kwargs["chunk_config"] == {"chunk_size": 400}
    assert kwargs["actor_id"] == authorizer.user_id


def test_create_rejects_unknown_field_and_empty_name(
    client: tuple[TestClient, FakeService, FakeAuthorizer], headers: dict[str, str]
) -> None:
    http, _service, _ = client

    unknown = http.post(
        "/api/v1/admin/knowledge-bases", json={"name": "x", "oops": 1}, headers=headers
    )
    empty = http.post("/api/v1/admin/knowledge-bases", json={"name": ""}, headers=headers)

    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "VALIDATION_ERROR"
    assert empty.status_code == 422


def test_create_maps_name_conflict(
    client: tuple[TestClient, FakeService, FakeAuthorizer], headers: dict[str, str]
) -> None:
    http, service, _ = client
    service.error = ConflictError(
        message="知识库名称已存在", code="KNOWLEDGE_BASE_NAME_TAKEN", details={"name": "产品库"}
    )

    resp = http.post("/api/v1/admin/knowledge-bases", json={"name": "产品库"}, headers=headers)

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "KNOWLEDGE_BASE_NAME_TAKEN"


def test_patch_forwards_only_what_was_sent(
    client: tuple[TestClient, FakeService, FakeAuthorizer], headers: dict[str, str]
) -> None:
    http, service, _ = client

    resp = http.patch(
        f"/api/v1/admin/knowledge-bases/{KB_ID}", json={"description": "新说明"}, headers=headers
    )

    assert resp.status_code == 200
    kind, kb_id, patch, _kw = service.calls[0]
    assert kind == "update" and kb_id == KB_ID
    assert patch == {"description": "新说明"}


def test_detail_maps_not_found(
    client: tuple[TestClient, FakeService, FakeAuthorizer], headers: dict[str, str]
) -> None:
    http, service, _ = client
    service.error = NotFoundError(
        message="知识库不存在", code="KNOWLEDGE_BASE_NOT_FOUND", details={"kb_id": str(KB_ID)}
    )

    resp = http.get(f"/api/v1/admin/knowledge-bases/{KB_ID}", headers=headers)

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "KNOWLEDGE_BASE_NOT_FOUND"


def test_delete_returns_204_and_maps_in_use(
    client: tuple[TestClient, FakeService, FakeAuthorizer], headers: dict[str, str]
) -> None:
    http, service, authorizer = client

    ok = http.delete(f"/api/v1/admin/knowledge-bases/{KB_ID}", headers=headers)
    assert ok.status_code == 204

    service.error = ConflictError(
        message="知识库仍在使用中", code="KNOWLEDGE_BASE_IN_USE", details={"documents": 2}
    )
    denied = http.delete(f"/api/v1/admin/knowledge-bases/{KB_ID}", headers=headers)

    assert denied.status_code == 409
    assert denied.json()["error"]["code"] == "KNOWLEDGE_BASE_IN_USE"
    kind, kb_id, kw = service.calls[0]
    assert kind == "delete" and kb_id == KB_ID
    assert kw["actor_id"] == authorizer.user_id


def test_links_round_trip(
    client: tuple[TestClient, FakeService, FakeAuthorizer], headers: dict[str, str]
) -> None:
    http, service, _ = client

    listed = http.get(f"/api/v1/admin/apps/{APP_ID}/knowledge-bases", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["items"][0]["linked"] is True

    put = http.put(
        f"/api/v1/admin/apps/{APP_ID}/knowledge-bases",
        json={"kb_ids": [str(KB_ID), str(OTHER_ID)]},
        headers=headers,
    )
    assert put.status_code == 200
    kind, app_id, kb_ids, _kw = service.calls[-1]
    assert kind == "set_links" and app_id == APP_ID
    assert list(kb_ids) == [KB_ID, OTHER_ID]


def test_permissions_and_token(
    client: tuple[TestClient, FakeService, FakeAuthorizer], headers: dict[str, str]
) -> None:
    http, service, authorizer = client

    missing = http.get("/api/v1/admin/knowledge-bases")
    assert missing.status_code == 401

    authorizer.deny = "kb.write"
    denied = http.post("/api/v1/admin/knowledge-bases", json={"name": "x"}, headers=headers)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "FORBIDDEN"
