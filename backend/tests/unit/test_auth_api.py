"""M4.1 ②：`/api/v1/admin/auth/*` 的协议层——参数、序列化、错误映射与认证头。

业务判定由 `test_auth_service.py` 覆盖；这里只证明路由把 service 的返回值与异常转成了契约里的形状。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from aics.core.errors import AccountLockedError, UnauthenticatedError
from aics.core.security import AuthedUser
from aics.main import create_app
from aics.modules.auth.schemas import SessionBody, TokenBody, UserBody


class FakeRuntime:
    def __init__(self, service: FakeService) -> None:
        self._service = service
        self.audit = None
        self.redis = None
        self.settings = None

    @asynccontextmanager
    async def sessions(self) -> AsyncIterator[None]:
        yield None

    def service(self, session: Any) -> FakeService:
        return self._service

    def authorizer(self) -> FakeAuthorizer:
        return FakeAuthorizer()


class FakeAuthorizer:
    async def authenticate(self, authorization: str | None) -> AuthedUser:
        if authorization != "Bearer good-token":
            raise UnauthenticatedError(details={"reason": "missing_token"})
        return AuthedUser(
            id=uuid4(), email="admin@example.com", display_name="管理员", is_super_admin=True
        )

    async def authorize(self, user: Any, *, app_id: Any, permission: str) -> None:
        return None


class FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.error: Exception | None = None

    def _maybe_raise(self) -> None:
        if self.error is not None:
            raise self.error

    async def login(self, email: str, password: str, *, meta: Any) -> SessionBody:
        self.calls.append(("login", email, password, meta.ip))
        self._maybe_raise()
        return SessionBody(
            access_token="a" * 32,
            refresh_token="r" * 32,
            expires_in=1800,
            user=UserBody(
                id=uuid4(),
                email=email,
                display_name="管理员",
                is_super_admin=True,
            ),
            apps=[],
        )

    async def refresh(self, token: str) -> TokenBody:
        self.calls.append(("refresh", token))
        self._maybe_raise()
        return TokenBody(access_token="a2" * 16, refresh_token="r2" * 16, expires_in=1800)

    async def logout(self, token: str | None) -> None:
        self.calls.append(("logout", token))

    async def me(self, user_id: Any) -> Any:
        self.calls.append(("me", user_id))
        return SimpleMe()

    async def change_password(
        self, user_id: Any, *, current: str, new: str, meta: Any
    ) -> TokenBody:
        self.calls.append(("change_password", current, new))
        self._maybe_raise()
        return TokenBody(access_token="a3" * 16, refresh_token="r3" * 16, expires_in=1800)


class SimpleMe:
    def model_dump(self, mode: str = "python") -> dict[str, Any]:
        return {
            "user": {
                "id": str(uuid4()),
                "email": "admin@example.com",
                "display_name": "管理员",
                "is_super_admin": True,
                "must_change_password": False,
            },
            "apps": [],
        }


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, FakeService]:
    service = FakeService()
    app = create_app()
    app.state.auth_runtime = FakeRuntime(service)
    app.state.authorizer = FakeAuthorizer()
    return TestClient(app), service


def test_login_returns_tokens(client: tuple[TestClient, FakeService]) -> None:
    http, service = client
    resp = http.post(
        "/api/v1/admin/auth/login",
        json={"email": "Admin@Example.com", "password": "correct horse battery"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 1800
    assert body["user"]["email"] == "admin@example.com"  # schema 已 to_lower
    assert service.calls[0][0] == "login"


def test_login_short_password_is_422(client: tuple[TestClient, FakeService]) -> None:
    http, _service = client
    resp = http.post("/api/v1/admin/auth/login", json={"email": "a@b.c", "password": "short"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_login_extra_field_is_422(client: tuple[TestClient, FakeService]) -> None:
    http, _service = client
    resp = http.post(
        "/api/v1/admin/auth/login",
        json={"email": "a@b.c", "password": "correct horse battery", "role": "super_admin"},
    )
    assert resp.status_code == 422


def test_locked_account_is_423_with_retry_after(client: tuple[TestClient, FakeService]) -> None:
    http, service = client
    service.error = AccountLockedError(
        details={"retry_after_seconds": 600}, headers={"Retry-After": "600"}
    )
    resp = http.post(
        "/api/v1/admin/auth/login",
        json={"email": "a@b.c", "password": "correct horse battery"},
    )
    assert resp.status_code == 423
    assert resp.headers["Retry-After"] == "600"
    assert resp.json()["error"]["code"] == "ACCOUNT_LOCKED"


def test_me_requires_a_bearer_token(client: tuple[TestClient, FakeService]) -> None:
    http, _service = client
    assert http.get("/api/v1/admin/auth/me").status_code == 401
    resp = http.get("/api/v1/admin/auth/me", headers={"authorization": "Bearer good-token"})
    assert resp.status_code == 200
    assert resp.json()["user"]["display_name"] == "管理员"


def test_refresh_and_logout(client: tuple[TestClient, FakeService]) -> None:
    http, service = client
    resp = http.post("/api/v1/admin/auth/refresh", json={"refresh_token": "r" * 32})
    assert resp.status_code == 200
    assert resp.json()["access_token"].startswith("a2")
    out = http.post("/api/v1/admin/auth/logout", json={"refresh_token": "r" * 32})
    assert out.status_code == 204
    assert service.calls[-1][0] == "logout"


def test_change_password_returns_a_fresh_pair(client: tuple[TestClient, FakeService]) -> None:
    http, service = client
    resp = http.post(
        "/api/v1/admin/auth/password",
        json={"current_password": "correct horse battery", "new_password": "brand-new-password"},
        headers={"authorization": "Bearer good-token"},
    )
    assert resp.status_code == 200
    assert resp.json()["refresh_token"].startswith("r3")
    assert service.calls[-1] == ("change_password", "correct horse battery", "brand-new-password")
