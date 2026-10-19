"""M3.6 ②：反馈路由（开放接口参考 §2）—— 参数、序列化、scope 与错误码。

fake 认证器 + fake 运行时跑真路由（`create_app()` 挂载）；业务判定由
`tests/unit/test_feedback_service.py` 覆盖，唯一键与覆盖写由集成用例覆盖。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from aics.core.errors import ForbiddenError, NotFoundError
from aics.core.security import Principal
from aics.main import create_app

HOST = "/api/v1/apps/{app_id}"


class FakeAuth:
    def __init__(self, scopes: tuple[str, ...] = ("chat",)) -> None:
        self.scopes = scopes
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
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[Any, ...]] = []

    async def submit(
        self, app_id: UUID, message_id: UUID, *, rating: int, comment: str | None = None
    ) -> Any:
        self.calls.append(("submit", app_id, message_id, rating, comment))
        if self.error is not None:
            raise self.error
        return _row(rating=rating)


class FakeRuntime:
    def __init__(self, service: FakeService) -> None:
        self.service_ = service

    @asynccontextmanager
    async def session(self) -> AsyncIterator[None]:
        yield None

    def service(self, session: Any) -> FakeService:  # noqa: ARG002 - 与 FeedbackRuntime 同形
        return self.service_


def _row(*, rating: int = 1) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(id=uuid4(), rating=rating)


def _client(
    service: FakeService, *, scopes: tuple[str, ...] = ("chat",)
) -> tuple[TestClient, FakeAuth]:
    app = create_app()
    auth = FakeAuth(scopes)
    app.state.authenticator = auth
    app.state.feedback_runtime = FakeRuntime(service)
    return TestClient(app, raise_server_exceptions=False), auth


def test_put_feedback_returns_id_and_rating() -> None:
    app_id, message_id = uuid4(), uuid4()
    service = FakeService()
    client, auth = _client(service)

    res = client.post(
        f"/api/v1/apps/{app_id}/messages/{message_id}/feedback",
        json={"rating": -1, "comment": "型号说错了"},
        headers={"Authorization": "Bearer aics_x"},
    )

    assert res.status_code == 200
    body = res.json()
    assert set(body) == {"id", "rating"}
    assert body["rating"] == -1
    assert res.headers["X-RateLimit-Remaining"] == "59"
    assert service.calls == [("submit", app_id, message_id, -1, "型号说错了")]
    assert auth.calls[0][1] == ("chat",)  # 只接受 chat scope


def test_comment_is_optional() -> None:
    service = FakeService()
    client, _ = _client(service)

    res = client.post(
        f"/api/v1/apps/{uuid4()}/messages/{uuid4()}/feedback",
        json={"rating": 1},
        headers={"Authorization": "Bearer aics_x"},
    )

    assert res.status_code == 200
    assert service.calls[0][4] is None


def test_missing_message_maps_to_404() -> None:
    service = FakeService(error=NotFoundError(details={"message_id": str(uuid4())}))
    client, _ = _client(service)

    res = client.post(
        f"/api/v1/apps/{uuid4()}/messages/{uuid4()}/feedback",
        json={"rating": 1},
        headers={"Authorization": "Bearer aics_x"},
    )

    assert res.status_code == 404
    assert res.json()["error"]["code"] == "NOT_FOUND"
    assert "message_id" in res.json()["error"]["details"]


@pytest.mark.parametrize("rating", [0, 2, -2, "up", 1.5, None])
def test_rating_must_be_one_or_minus_one(rating: Any) -> None:
    service = FakeService()
    client, _ = _client(service)

    res = client.post(
        f"/api/v1/apps/{uuid4()}/messages/{uuid4()}/feedback",
        json={"rating": rating},
        headers={"Authorization": "Bearer aics_x"},
    )

    assert res.status_code == 422
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"
    assert service.calls == []


def test_unknown_field_is_rejected() -> None:
    client, _ = _client(FakeService())

    res = client.post(
        f"/api/v1/apps/{uuid4()}/messages/{uuid4()}/feedback",
        json={"rating": 1, "source": "admin"},
        headers={"Authorization": "Bearer aics_x"},
    )

    assert res.status_code == 422
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"


def test_comment_longer_than_the_limit_is_rejected() -> None:
    client, _ = _client(FakeService())

    res = client.post(
        f"/api/v1/apps/{uuid4()}/messages/{uuid4()}/feedback",
        json={"rating": 1, "comment": "长" * 2001},
        headers={"Authorization": "Bearer aics_x"},
    )

    assert res.status_code == 422


def test_handoff_only_key_is_forbidden() -> None:
    service = FakeService()
    client, _ = _client(service, scopes=("handoff:write",))

    res = client.post(
        f"/api/v1/apps/{uuid4()}/messages/{uuid4()}/feedback",
        json={"rating": 1},
        headers={"Authorization": "Bearer aics_x"},
    )

    assert res.status_code == 403
    assert res.json()["error"]["code"] == "FORBIDDEN"
    assert service.calls == []
