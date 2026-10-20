"""M4.2 ②：`/api/v1/admin/apps*` 的协议层——参数解析、鉴权与错误映射。

业务判定在 `test_apps_admin_service.py`；这里只证明 router 把路径/查询参数与
`expected_config_version` 正确地交给了 service，并把异常翻成约定里的形状。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from aics.core.errors import ConfigVersionConflictError, ForbiddenError, UnauthenticatedError
from aics.core.security import AuthedUser
from aics.main import create_app

APP_ID = uuid4()


class FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.error: Exception | None = None

    def _maybe_raise(self) -> None:
        if self.error is not None:
            raise self.error

    async def list_apps(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("list_apps", kwargs))
        self._maybe_raise()
        return [{"id": str(APP_ID), "name": "验收应用", "stats": {"documents": 2}}]

    async def detail(self, app_id: Any) -> dict[str, Any]:
        self.calls.append(("detail", app_id))
        self._maybe_raise()
        return {"id": str(app_id), "name": "验收应用", "config_version": 3}

    async def update(self, app_id: Any, patch: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("update", app_id, patch, kwargs))
        self._maybe_raise()
        return {"id": str(app_id), "config_version": 4, "retokenize_job_id": None}

    async def create(self, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("create", payload, kwargs))
        self._maybe_raise()
        return {"id": str(APP_ID), "name": payload.get("name", "新建")}

    async def copy(self, app_id: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("copy", app_id, kwargs))
        self._maybe_raise()
        return {"id": str(APP_ID), "name": "售后客服（副本）"}

    async def delete(self, app_id: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("delete", app_id, kwargs))
        self._maybe_raise()
        return {"job_id": str(uuid4()), "revoked_keys": 1, "closed_conversations": 0}

    async def system_settings(self) -> dict[str, Any]:
        self.calls.append(("system_settings",))
        self._maybe_raise()
        return {"default_session_config": {"idle_close_minutes": 10}}

    async def update_system_settings(
        self, payload: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        self.calls.append(("update_system_settings", payload, kwargs))
        self._maybe_raise()
        return {"default_session_config": payload["default_session_config"]}


class FakeRuntime:
    def __init__(self, service: FakeService) -> None:
        self._service = service
        self.settings = SimpleNamespace(
            models=SimpleNamespace(public=lambda: [{"name": "deepseek-chat", "kind": "chat"}])
        )

    @asynccontextmanager
    async def session(self) -> AsyncIterator[None]:
        yield None

    async def service(self, _session: Any) -> FakeService:
        return self._service


class FakeAuthorizer:
    def __init__(self, *, deny: str | None = None) -> None:
        self.deny = deny

    async def authenticate(self, authorization: str | None) -> AuthedUser:
        if authorization != "Bearer good-token":
            raise UnauthenticatedError(details={"reason": "missing_token"})
        return AuthedUser(
            id=uuid4(), email="admin@example.com", display_name="管理员", is_super_admin=True
        )

    async def authorize(self, user: Any, *, app_id: Any, permission: str) -> None:
        if self.deny is not None and permission == self.deny:
            raise ForbiddenError(details={"reason": "missing_permission", "permission": permission})


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, FakeService]:
    service = FakeService()
    app = create_app()
    app.state.apps_admin_runtime = FakeRuntime(service)
    app.state.authorizer = FakeAuthorizer()
    return TestClient(app), service


@pytest.fixture
def headers() -> dict[str, str]:
    return {"authorization": "Bearer good-token"}


def test_list_apps_passes_the_caller_and_paging(
    client: tuple[TestClient, FakeService], headers: dict[str, str]
) -> None:
    http, service = client

    resp = http.get("/api/v1/admin/apps?limit=20&offset=5", headers=headers)

    assert resp.status_code == 200
    assert resp.json()["items"][0]["stats"] == {"documents": 2}
    kind, kwargs = service.calls[0]
    assert kind == "list_apps"
    assert kwargs["limit"] == 20 and kwargs["offset"] == 5
    assert kwargs["is_super_admin"] is True
    assert kwargs["user_id"] is not None


def test_detail_returns_the_service_body(
    client: tuple[TestClient, FakeService], headers: dict[str, str]
) -> None:
    http, _service = client

    resp = http.get(f"/api/v1/admin/apps/{APP_ID}", headers=headers)

    assert resp.status_code == 200
    assert resp.json()["config_version"] == 3


def test_patch_strips_the_expected_version_and_passes_the_patch(
    client: tuple[TestClient, FakeService], headers: dict[str, str]
) -> None:
    http, service = client

    resp = http.patch(
        f"/api/v1/admin/apps/{APP_ID}",
        headers=headers,
        json={"name": "新名字", "expected_config_version": 3},
    )

    assert resp.status_code == 200
    _kind, app_id, patch, kwargs = service.calls[0]
    assert app_id == APP_ID
    assert patch == {"name": "新名字"}
    assert kwargs["expected_version"] == 3
    assert kwargs["actor_id"] is not None


def test_patch_rejects_a_non_integer_expected_version(
    client: tuple[TestClient, FakeService], headers: dict[str, str]
) -> None:
    http, service = client

    resp = http.patch(
        f"/api/v1/admin/apps/{APP_ID}",
        headers=headers,
        json={"name": "新名字", "expected_config_version": "三"},
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["details"]["errors"][0]["loc"] == ["expected_config_version"]
    assert service.calls == []


def test_patch_surfaces_the_version_conflict(
    client: tuple[TestClient, FakeService], headers: dict[str, str]
) -> None:
    http, service = client
    service.error = ConfigVersionConflictError(details={"expected": 3, "current": 4})

    resp = http.patch(
        f"/api/v1/admin/apps/{APP_ID}",
        headers=headers,
        json={"name": "新名字", "expected_config_version": 3},
    )

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "CONFIG_VERSION_CONFLICT"
    assert resp.json()["error"]["details"] == {"expected": 3, "current": 4}


def test_models_and_presets_come_from_the_runtime(
    client: tuple[TestClient, FakeService], headers: dict[str, str]
) -> None:
    http, _service = client

    models = http.get("/api/v1/admin/models", headers=headers)
    presets = http.get("/api/v1/admin/chunk-presets", headers=headers)

    assert models.status_code == 200
    assert models.json() == [{"name": "deepseek-chat", "kind": "chat"}]
    assert presets.status_code == 200
    assert {"name", "label", "config"} <= set(presets.json()[0])
    assert "chunk_size" in presets.json()[0]["config"]


def test_create_returns_201_and_passes_the_body(
    client: tuple[TestClient, FakeService], headers: dict[str, str]
) -> None:
    http, service = client

    resp = http.post(
        "/api/v1/admin/apps", headers=headers, json={"name": "新应用", "greeting": "你好"}
    )

    assert resp.status_code == 201
    assert resp.json()["name"] == "新应用"
    kind, payload, kwargs = service.calls[0]
    assert kind == "create"
    assert payload == {"name": "新应用", "greeting": "你好"}
    assert kwargs["actor_id"] is not None


def test_copy_returns_201_with_the_suffixed_name(
    client: tuple[TestClient, FakeService], headers: dict[str, str]
) -> None:
    http, service = client

    resp = http.post(f"/api/v1/admin/apps/{APP_ID}/copy", headers=headers)

    assert resp.status_code == 201
    assert resp.json()["name"] == "售后客服（副本）"
    assert service.calls[0][:2] == ("copy", APP_ID)


def test_delete_returns_202_with_the_job(
    client: tuple[TestClient, FakeService], headers: dict[str, str]
) -> None:
    http, service = client

    resp = http.delete(f"/api/v1/admin/apps/{APP_ID}", headers=headers)

    assert resp.status_code == 202
    body = resp.json()
    assert set(body) == {"job_id", "revoked_keys", "closed_conversations"}
    assert service.calls[0][:2] == ("delete", APP_ID)


def test_lifecycle_endpoints_require_their_own_permissions(
    client: tuple[TestClient, FakeService], headers: dict[str, str]
) -> None:
    http, service = client
    http.app.state.authorizer = FakeAuthorizer(deny="app.create")

    created = http.post("/api/v1/admin/apps", headers=headers, json={"name": "新应用"})
    copied = http.post(f"/api/v1/admin/apps/{APP_ID}/copy", headers=headers)
    removed = http.delete(f"/api/v1/admin/apps/{APP_ID}", headers=headers)

    assert created.status_code == 403
    assert created.json()["error"]["details"]["permission"] == "app.create"
    assert copied.status_code == 201
    assert removed.status_code == 202
    assert [call[0] for call in service.calls] == ["copy", "delete"]


def test_denied_permission_is_403(
    client: tuple[TestClient, FakeService], headers: dict[str, str]
) -> None:
    http, _service = client
    http.app.state.authorizer = FakeAuthorizer(deny="app.read")

    resp = http.get(f"/api/v1/admin/apps/{APP_ID}", headers=headers)

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"


def test_system_settings_read_returns_the_platform_default(
    client: tuple[TestClient, FakeService], headers: dict[str, str]
) -> None:
    http, service = client

    resp = http.get("/api/v1/admin/system/settings", headers=headers)

    assert resp.status_code == 200
    assert resp.json() == {"default_session_config": {"idle_close_minutes": 10}}
    assert service.calls == [("system_settings",)]


def test_system_settings_patch_passes_the_body_and_the_actor(
    client: tuple[TestClient, FakeService], headers: dict[str, str]
) -> None:
    http, service = client
    payload = {"default_session_config": {"idle_close_minutes": 20}}

    resp = http.patch("/api/v1/admin/system/settings", headers=headers, json=payload)

    assert resp.status_code == 200
    assert resp.json() == payload
    name, body, kwargs = service.calls[0]
    assert name == "update_system_settings" and body == payload
    assert kwargs["actor_id"] is not None and kwargs["meta"].request_id


def test_system_settings_requires_its_own_permission(
    client: tuple[TestClient, FakeService], headers: dict[str, str]
) -> None:
    http, service = client
    http.app.state.authorizer = FakeAuthorizer(deny="system.write")

    denied = http.patch(
        "/api/v1/admin/system/settings",
        headers=headers,
        json={"default_session_config": {"idle_close_minutes": 20}},
    )
    http.app.state.authorizer = FakeAuthorizer(deny="system.read")
    read = http.get("/api/v1/admin/system/settings", headers=headers)

    assert denied.status_code == 403
    assert denied.json()["error"]["details"]["permission"] == "system.write"
    assert read.status_code == 403
    assert read.json()["error"]["details"]["permission"] == "system.read"
    assert service.calls == []
