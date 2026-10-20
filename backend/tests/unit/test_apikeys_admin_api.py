"""M4.5 ②：`/api/v1/admin/apps/{app_id}/keys*` 的协议层。

业务规则在 `test_apikeys_admin_service.py`；这里只证明路由把参数原样交给 service、
`response_model` 生效、以及 401 / 403 / 404 / 422 的形状。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from aics.core.errors import ForbiddenError, NotFoundError, UnauthenticatedError
from aics.core.security import AuthedUser
from aics.main import create_app
from aics.modules.apikeys.admin_service import KEEP
from aics.modules.apikeys.service import DEFAULT_RPM_LIMIT

APP_ID = uuid4()
KEY_ID = uuid4()
USER_ID = uuid4()


def key_body(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": str(KEY_ID),
        "name": "生产接入",
        "prefix": "aics_abcdefg",
        "hint": "aics_abcdefg…defg",
        "scopes": ["chat"],
        "rpm_limit": DEFAULT_RPM_LIMIT,
        "daily_quota": None,
        "ip_allowlist": [],
        "last_used_at": None,
        "expires_at": None,
        "revoked_at": None,
        "created_at": "2026-10-20T10:00:00+00:00",
        "status": "active",
    }
    body.update(over)
    return body


class FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.error: Exception | None = None

    def _maybe_raise(self) -> None:
        if self.error is not None:
            raise self.error

    async def list_keys(self, app_id: Any) -> list[dict[str, Any]]:
        self.calls.append(("list_keys", app_id))
        self._maybe_raise()
        return [key_body()]

    async def create_key(self, app_id: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("create_key", app_id, kwargs))
        self._maybe_raise()
        return key_body(key="aics_S" + "ecret" * 6)

    async def update_key(self, app_id: Any, key_id: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("update_key", app_id, key_id, kwargs))
        self._maybe_raise()
        return key_body(rpm_limit=kwargs.get("rpm_limit") or DEFAULT_RPM_LIMIT)

    async def revoke_key(self, app_id: Any, key_id: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("revoke_key", app_id, key_id, kwargs))
        self._maybe_raise()
        return key_body(status="revoked", revoked_at="2026-10-20T11:00:00+00:00")

    async def example(self, app_id: Any, key_id: Any, *, base_url: str) -> dict[str, Any]:
        self.calls.append(("example", app_id, key_id, base_url))
        self._maybe_raise()
        url = f"{base_url}/api/v1/apps/{app_id}"
        return {
            "key_id": str(key_id),
            "name": "生产接入",
            "curl": f'curl -H "Authorization: Bearer <YOUR_API_KEY>" "{url}/documents"',
            "python": "import httpx",
            "javascript": "await fetch(...)",
            "url": url,
        }


class FakeRuntime:
    def __init__(self, service: FakeService) -> None:
        self._service = service

    @asynccontextmanager
    async def session(self) -> AsyncIterator[None]:
        yield None

    def service(self, _session: Any) -> FakeService:
        return self._service


class FakeAuthorizer:
    def __init__(self, *, deny: str | None = None) -> None:
        self.deny = deny

    async def authenticate(self, authorization: str | None) -> AuthedUser:
        if authorization != "Bearer good-token":
            raise UnauthenticatedError(details={"reason": "missing_token"})
        return AuthedUser(
            id=USER_ID, email="admin@example.com", display_name="管理员", is_super_admin=False
        )

    async def authorize(self, user: Any, *, app_id: Any, permission: str) -> None:
        if self.deny is not None and permission == self.deny:
            raise ForbiddenError(details={"reason": "missing_permission", "permission": permission})


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, FakeService]:
    service = FakeService()
    app = create_app()
    app.state.apikeys_admin_runtime = FakeRuntime(service)
    app.state.authorizer = FakeAuthorizer()
    return TestClient(app), service


@pytest.fixture
def headers() -> dict[str, str]:
    return {"authorization": "Bearer good-token"}


BASE = f"/api/v1/admin/apps/{APP_ID}/keys"


def test_list_keys_is_masked_and_typed(
    client: tuple[TestClient, FakeService], headers: dict[str, str]
) -> None:
    http, service = client
    response = http.get(BASE, headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["hint"].endswith("defg")
    assert "key" not in body["items"][0]
    assert service.calls[0] == ("list_keys", UUID(str(APP_ID)))


def test_create_returns_the_plaintext_with_201(
    client: tuple[TestClient, FakeService], headers: dict[str, str]
) -> None:
    http, service = client
    response = http.post(
        BASE,
        headers=headers,
        json={
            "name": "生产接入",
            "scopes": ["chat"],
            "rpm_limit": 30,
            "ip_allowlist": ["10.0.0.0/8"],
        },
    )
    assert response.status_code == 201
    assert response.json()["key"].startswith("aics_")
    call = service.calls[0]
    assert call[0] == "create_key"
    passed = call[2]
    assert passed["name"] == "生产接入"
    assert passed["scopes"] == ["chat"]
    assert passed["rpm_limit"] == 30
    assert passed["ip_allowlist"] == ["10.0.0.0/8"]
    assert passed["actor_id"] == USER_ID
    assert passed["meta"].request_id is None or isinstance(passed["meta"].request_id, str)


def test_create_rejects_an_empty_name(
    client: tuple[TestClient, FakeService], headers: dict[str, str]
) -> None:
    http, _service = client
    response = http.post(BASE, headers=headers, json={"name": "", "scopes": ["chat"]})
    assert response.status_code == 422


def test_create_rejects_unknown_fields(
    client: tuple[TestClient, FakeService], headers: dict[str, str]
) -> None:
    http, _service = client
    response = http.post(BASE, headers=headers, json={"name": "x", "scopes": ["chat"], "bogus": 1})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_patch_only_passes_the_provided_fields(
    client: tuple[TestClient, FakeService], headers: dict[str, str]
) -> None:
    http, service = client
    response = http.patch(f"{BASE}/{KEY_ID}", headers=headers, json={"rpm_limit": 5})
    assert response.status_code == 200
    passed = service.calls[0][3]
    assert passed["rpm_limit"] == 5
    assert passed["daily_quota"] is KEEP
    assert passed["ip_allowlist"] is KEEP


def test_patch_clears_the_quota_with_null(
    client: tuple[TestClient, FakeService], headers: dict[str, str]
) -> None:
    http, service = client
    response = http.patch(f"{BASE}/{KEY_ID}", headers=headers, json={"daily_quota": None})
    assert response.status_code == 200
    assert service.calls[0][3]["daily_quota"] is None


def test_delete_revokes_and_returns_the_row(
    client: tuple[TestClient, FakeService], headers: dict[str, str]
) -> None:
    http, service = client
    response = http.delete(f"{BASE}/{KEY_ID}", headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "revoked"
    assert service.calls[0][0] == "revoke_key"
    assert service.calls[0][3]["actor_id"] == USER_ID


def test_example_passes_the_request_base_url(
    client: tuple[TestClient, FakeService], headers: dict[str, str]
) -> None:
    http, service = client
    response = http.get(f"{BASE}/{KEY_ID}/example", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["url"].endswith(f"/api/v1/apps/{APP_ID}")
    assert "<YOUR_API_KEY>" in body["curl"]
    assert service.calls[0][3] == "http://testserver"


def test_missing_key_maps_to_404_with_details(
    client: tuple[TestClient, FakeService], headers: dict[str, str]
) -> None:
    http, service = client
    service.error = NotFoundError(message="API Key 不存在", details={"key_id": str(KEY_ID)})
    response = http.delete(f"{BASE}/{KEY_ID}", headers=headers)
    assert response.status_code == 404
    body = response.json()["error"]
    assert body["code"] == "NOT_FOUND"
    assert body["details"] == {"key_id": str(KEY_ID)}


def test_missing_token_is_401(client: tuple[TestClient, FakeService]) -> None:
    http, _service = client
    assert http.get(BASE).status_code == 401


def test_missing_permission_is_403(
    client: tuple[TestClient, FakeService], headers: dict[str, str]
) -> None:
    http, _service = client
    http.app.state.authorizer = FakeAuthorizer(deny="key.write")  # type: ignore[attr-defined]
    response = http.post(BASE, headers=headers, json={"name": "x", "scopes": ["chat"]})
    assert response.status_code == 403
