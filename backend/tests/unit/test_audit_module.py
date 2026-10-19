"""M4.1 ③：审计模块——游标编解码、分页与协议层。

分页语义：`id` 降序、`page_size + 1` 多取一行判断是否还有下一页；游标是 `a:<id>` 的 base64url。
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from aics.core.errors import ForbiddenError, ValidationAppError
from aics.core.security import AuthedUser
from aics.main import create_app
from aics.modules.audit import service as audit_service
from aics.modules.audit.service import AuditService, _decode, _encode


def test_cursor_roundtrip() -> None:
    token = _encode(42)
    assert _decode(token) == 42
    assert "=" not in token


@pytest.mark.parametrize("raw", ["", "%%%", base64.urlsafe_b64encode(b"b:7").decode(), _encode(0)])
def test_invalid_cursor_is_422(raw: str) -> None:
    with pytest.raises(ValidationAppError) as err:
        _decode(raw)
    assert err.value.status == 422
    assert err.value.details["errors"][0]["loc"] == ["query", "cursor"]


class _Row:
    def __init__(self, entry_id: int) -> None:
        self.id = entry_id
        self.action = "user.create"
        self.actor_type = "user"
        self.actor_id = None
        self.app_id = None
        self.target_type = "user"
        self.target_id = None
        self.diff = {"email": "a@b.c"}
        self.ip = "127.0.0.1"
        self.user_agent = "pytest"
        self.request_id = "req-1"
        self.created_at = None


class FakeRepo:
    def __init__(self, ids: list[int]) -> None:
        self.ids = ids
        self.calls: list[dict[str, Any]] = []

    async def page(self, **kwargs: Any) -> list[_Row]:
        self.calls.append(kwargs)
        return [_Row(entry_id) for entry_id in self.ids]

    async def actions(self) -> list[str]:
        return ["auth.login", "user.create"]

    def body(self, row: _Row) -> dict[str, Any]:
        return {"id": row.id, "action": row.action, "app_id": None}


async def test_page_has_no_cursor_when_the_batch_is_not_full() -> None:
    repo = FakeRepo([3, 2])
    payload = await AuditService(None, repo=repo).page(page_size=2)  # type: ignore[arg-type]
    assert [item["id"] for item in payload["items"]] == [3, 2]
    assert payload["next_cursor"] is None
    assert repo.calls[0]["limit"] == 3


async def test_page_returns_cursor_when_more_rows_exist() -> None:
    repo = FakeRepo([9, 8, 7])
    payload = await AuditService(None, repo=repo).page(page_size=2)  # type: ignore[arg-type]
    assert [item["id"] for item in payload["items"]] == [9, 8]
    assert payload["next_cursor"] == _encode(8)


async def test_page_decodes_cursor_into_before_id() -> None:
    repo = FakeRepo([])
    await AuditService(None, repo=repo).page(cursor=_encode(5))  # type: ignore[arg-type]
    assert repo.calls[0]["before_id"] == 5


def test_cursor_prefix_is_documented() -> None:
    assert audit_service.CURSOR_PREFIX == "a:"


# ------------------------------------------------------------------ 协议层
class FakeAuditService:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.error: Exception | None = None

    async def page(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("page", kwargs))
        if self.error is not None:
            raise self.error
        return {"items": [{"id": 3, "action": "auth.login"}], "next_cursor": None}

    async def actions(self) -> list[str]:
        self.calls.append(("actions",))
        return ["auth.login"]


class FakeRuntime:
    def __init__(self, service: FakeAuditService) -> None:
        self._service = service

    @asynccontextmanager
    async def session(self) -> AsyncIterator[None]:
        yield None

    def service(self, session: Any) -> FakeAuditService:
        return self._service


class FakeAuthorizer:
    def __init__(self, *, deny: bool = False) -> None:
        self.deny = deny

    async def authenticate(self, authorization: str | None) -> AuthedUser:
        return AuthedUser(
            id=uuid4(), email="admin@example.com", display_name="管理员", is_super_admin=True
        )

    async def authorize(self, user: Any, *, app_id: Any, permission: str) -> None:
        if self.deny:
            raise ForbiddenError(details={"permission": permission})


@pytest.fixture
def client() -> tuple[TestClient, FakeAuditService, dict[str, Any]]:
    service = FakeAuditService()
    app = create_app()
    runtime = FakeRuntime(service)
    app.state.audit_runtime = runtime
    app.state.authorizer = FakeAuthorizer()
    state: dict[str, Any] = {"runtime": runtime}
    return TestClient(app), service, state


def test_audit_list_returns_items(
    client: tuple[TestClient, FakeAuditService, dict[str, Any]],
) -> None:
    http, service, _state = client
    resp = http.get("/api/v1/admin/audit?page_size=10")
    assert resp.status_code == 200
    assert resp.json()["items"][0]["action"] == "auth.login"
    assert service.calls[0][1]["page_size"] == 10


def test_audit_list_forbidden(client: tuple[TestClient, FakeAuditService, dict[str, Any]]) -> None:
    http, _service, state = client
    state["runtime"].service  # noqa: B018 - keep the runtime referenced
    http.app.state.authorizer = FakeAuthorizer(deny=True)  # type: ignore[attr-defined]
    resp = http.get("/api/v1/admin/audit")
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"


def test_audit_actions(client: tuple[TestClient, FakeAuditService, dict[str, Any]]) -> None:
    http, _service, _state = client
    resp = http.get("/api/v1/admin/audit/actions")
    assert resp.status_code == 200
    assert resp.json()["items"] == ["auth.login"]
