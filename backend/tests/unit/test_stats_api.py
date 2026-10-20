"""M4.6 ②：统计接口的协议层（`response_model`、参数、401/403/422）。

真实聚合与查询由集成用例覆盖；这里只证明路由把 `days` / `app_id` 原样交给 service，
且返回体符合 `response_model`（多出的键会被过滤）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from aics.core.errors import ForbiddenError, UnauthenticatedError
from aics.core.security import AuthedUser
from aics.main import create_app

APP_ID = uuid4()
USER_ID = uuid4()


def totals(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "conversations": 3,
        "messages": 6,
        "answered": 5,
        "refused": 1,
        "handoff": 0,
        "errors": 0,
        "handoff_requested": 1,
        "handoff_accepted": 1,
        "handoff_released": 1,
        "handoff_queue_timeout": 0,
        "handoff_offline": 1,
        "handoff_accepted_idle_closed": 0,
        "human_agent_messages": 2,
        "idle_closed": 1,
        "negative_feedback": 0,
        "tokens_in": 100,
        "tokens_out": 200,
    }
    base.update(over)
    return base


def daily_row(day: str = "2026-10-20") -> dict[str, Any]:
    return {
        "date": day,
        **totals(),
        "handoff_accept_p50_s": 12,
        "latency_p50_ms": 900,
        "latency_p95_ms": 2100,
        "first_token_p95_ms": None,
        "intent_distribution": {"kb_qa": 4},
    }


class FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def overview(self, *, days: int) -> dict[str, Any]:
        self.calls.append(("overview", days))
        return {
            "days": days,
            "since": "2026-10-14",
            "until": "2026-10-20",
            "totals": totals(),
            "apps": [{"app_id": str(APP_ID), "name": "客服 A", **totals()}],
            "series": [daily_row()],
            "intent_distribution": {"kb_qa": 4},
            # 多出来的键会被 response_model 过滤掉
            "extra": "dropped",
        }

    async def series(self, app_id: UUID, *, days: int) -> dict[str, Any]:
        self.calls.append(("series", app_id, days))
        return {
            "app_id": str(app_id),
            "days": days,
            "since": "2026-10-14",
            "until": "2026-10-20",
            "items": [daily_row()],
        }


@asynccontextmanager
async def _session() -> AsyncIterator[Any]:
    yield object()


class FakeRuntime:
    def __init__(self, service: FakeService) -> None:
        self._service = service

    def session(self) -> Any:
        return _session()

    def service(self, session: Any) -> FakeService:
        return self._service


class FakeAuthorizer:
    def __init__(self, *, deny: str | None = None, super_admin: bool = True) -> None:
        self.deny = deny
        self.super_admin = super_admin

    async def authenticate(self, authorization: str | None) -> AuthedUser:
        if authorization != "Bearer good-token":
            raise UnauthenticatedError(details={"reason": "missing_token"})
        return AuthedUser(
            id=USER_ID,
            email="admin@example.com",
            display_name="管理员",
            is_super_admin=self.super_admin,
        )

    async def authorize(self, user: Any, *, app_id: Any, permission: str) -> None:
        if self.deny is not None and permission == self.deny:
            raise ForbiddenError(details={"reason": "missing_permission", "permission": permission})


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, FakeService]:
    service = FakeService()
    app = create_app()
    app.state.stats_runtime = FakeRuntime(service)
    app.state.authorizer = FakeAuthorizer()
    return TestClient(app), service


@pytest.fixture
def headers() -> dict[str, str]:
    return {"authorization": "Bearer good-token"}


def test_overview_returns_totals_and_filters_extra_keys(
    rig: tuple[TestClient, FakeService], headers: dict[str, str]
) -> None:
    http, service = rig
    response = http.get("/api/v1/admin/stats/overview", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert "extra" not in body
    assert body["days"] == 7
    assert body["totals"]["conversations"] == 3
    assert body["series"][0]["date"] == "2026-10-20"
    assert body["apps"][0]["name"] == "客服 A"
    assert service.calls == [("overview", 7)]


def test_overview_passes_days(rig: tuple[TestClient, FakeService], headers: dict[str, str]) -> None:
    http, service = rig
    assert http.get("/api/v1/admin/stats/overview?days=30", headers=headers).status_code == 200
    assert service.calls == [("overview", 30)]


@pytest.mark.parametrize("days", [0, 91, -1])
def test_overview_rejects_days_out_of_range(
    rig: tuple[TestClient, FakeService], headers: dict[str, str], days: int
) -> None:
    http, service = rig
    response = http.get(f"/api/v1/admin/stats/overview?days={days}", headers=headers)
    assert response.status_code == 422
    assert service.calls == []


def test_app_stats_passes_app_id(
    rig: tuple[TestClient, FakeService], headers: dict[str, str]
) -> None:
    http, service = rig
    response = http.get(f"/api/v1/admin/apps/{APP_ID}/stats?days=3", headers=headers)
    assert response.status_code == 200
    assert response.json()["app_id"] == str(APP_ID)
    assert service.calls == [("series", APP_ID, 3)]


def test_stats_read_denied(rig: tuple[TestClient, FakeService], headers: dict[str, str]) -> None:
    http, _ = rig
    http.app.state.authorizer = FakeAuthorizer(deny="stats.read")
    response = http.get("/api/v1/admin/stats/overview", headers=headers)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


def test_missing_token_is_401(rig: tuple[TestClient, FakeService]) -> None:
    http, _ = rig
    response = http.get("/api/v1/admin/stats/overview")
    assert response.status_code == 401


def test_platform_overview_is_super_admin_only(
    rig: tuple[TestClient, FakeService], headers: dict[str, str]
) -> None:
    """`require_perm` 对 app_id=None 的分支：非超级管理员一律 403（Authorizer 的真实语义）。"""

    class StrictAuthorizer(FakeAuthorizer):
        async def authorize(self, user: Any, *, app_id: Any, permission: str) -> None:
            if app_id is None:
                raise ForbiddenError(
                    details={"permission": permission, "reason": "super_admin_only"}
                )

    http, _ = rig
    http.app.state.authorizer = StrictAuthorizer(super_admin=False)
    response = http.get("/api/v1/admin/stats/overview", headers=headers)
    assert response.status_code == 403
    assert response.json()["error"]["details"]["reason"] == "super_admin_only"
