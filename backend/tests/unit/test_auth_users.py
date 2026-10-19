"""M4.1 ③：用户 CRUD 与应用成员授权（服务层 + 协议层）。

两件必须成立的事：每次写都留审计（`user.create` / `user.update` / `user.delete` /
`member.set` / `member.remove`），以及改密 / 禁用 / 改角色之后缓存立刻失效（ISSUE-72）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from aics.core.audit import AuditEntry
from aics.core.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UnauthenticatedError,
    ValidationAppError,
)
from aics.core.security import AuthedUser
from aics.main import create_app
from aics.modules.auth.service import AuthService, RequestMeta

PREFIX = "aics:"


class FakeRedis:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete(self, *keys: str) -> int:
        self.deleted.extend(keys)
        return len(keys)


class FakeAudit:
    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    async def write(self, session: Any, entry: AuditEntry) -> None:
        self.entries.append(entry)


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.flushes = 0

    async def commit(self) -> None:
        self.commits += 1

    async def flush(self) -> None:
        self.flushes += 1


def _user(**over: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid4(),
        "email": "user@example.com",
        "display_name": "用户",
        "is_super_admin": False,
        "must_change_password": False,
        "status": "active",
        "token_version": 0,
        "password_hash": "hash",
        "deleted_at": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


class FakeRepo:
    def __init__(
        self, *, users: list[SimpleNamespace] | None = None, apps: list[UUID] | None = None
    ):
        self.rows = users or []
        self.apps = apps or []
        self.links: dict[tuple[UUID, UUID], str] = {}
        self.app_members: list[tuple[SimpleNamespace, str]] = []
        self.bumped = 0

    async def user_by_email(self, email: str) -> Any:
        for row in self.rows:
            if row.email == email.lower():
                return row
        return None

    async def user(self, user_id: UUID) -> Any:
        for row in self.rows:
            if row.id == user_id:
                return row
        return None

    async def users(self, *, q: str | None = None, limit: int = 100) -> list[Any]:
        return list(self.rows)

    async def create_user(self, *, email: str, display_name: str, password_hash: str) -> Any:
        row = _user(email=email, display_name=display_name, password_hash=password_hash)
        row.must_change_password = True
        self.rows.append(row)
        return row

    async def bump_token_version(self, user: Any) -> int:
        user.token_version += 1
        self.bumped += 1
        return user.token_version

    async def role(self, role_id: str) -> Any:
        if role_id in ("app_admin", "viewer", "super_admin"):
            return SimpleNamespace(id=role_id, permissions=[])
        return None

    async def app(self, app_id: UUID) -> Any:
        if app_id in self.apps:
            return SimpleNamespace(id=app_id, name="应用")
        return None

    async def set_membership(self, *, user_id: UUID, app_id: UUID, role_id: str) -> Any:
        self.links[(user_id, app_id)] = role_id
        return SimpleNamespace(user_id=user_id, app_id=app_id, role_id=role_id)

    async def delete_membership(self, *, user_id: UUID, app_id: UUID) -> bool:
        return self.links.pop((user_id, app_id), None) is not None

    async def members(self, app_id: UUID) -> list[tuple[Any, Any, Any]]:
        return [(SimpleNamespace(role_id=r), u, None) for u, r in self.app_members]


def _service(
    repo: FakeRepo, *, redis: FakeRedis | None = None
) -> tuple[AuthService, FakeSession, FakeAudit]:
    session = FakeSession()
    audit = FakeAudit()
    svc = AuthService(session, audit=audit, redis=redis)  # type: ignore[arg-type]
    svc.repo = repo  # type: ignore[assignment]
    return svc, session, audit


async def test_create_user_hashes_password_and_audits() -> None:
    repo = FakeRepo()
    svc, session, audit = _service(repo)
    body = await svc.create_user(
        email="New@Example.com",
        display_name="新同事",
        password="correct horse battery",
        meta=RequestMeta(ip="127.0.0.1"),
    )
    assert body.email == "new@example.com"
    assert body.must_change_password is True
    assert repo.rows[0].password_hash != "correct horse battery"
    assert audit.entries[0].action == "user.create"
    assert audit.entries[0].diff == {
        "email": "new@example.com",
        "display_name": "新同事",
        "is_super_admin": False,
    }
    assert session.commits == 1


async def test_create_user_rejects_duplicate_email() -> None:
    existing = _user(email="dup@example.com")
    svc, _session, _audit = _service(FakeRepo(users=[existing]))
    with pytest.raises(ConflictError) as err:
        await svc.create_user(
            email="dup@example.com", display_name="重复", password="correct horse battery"
        )
    assert err.value.status == 409


async def test_update_user_name_does_not_bump_tokens() -> None:
    user = _user()
    repo = FakeRepo(users=[user])
    redis = FakeRedis()
    svc, _session, audit = _service(repo, redis=redis)
    body = await svc.update_user(user.id, display_name="改名了")
    assert body.display_name == "改名了"
    assert repo.bumped == 0
    assert redis.deleted == []
    assert audit.entries[0].action == "user.update"
    assert audit.entries[0].diff == {
        "before": {"display_name": "用户"},
        "after": {"display_name": "改名了"},
    }


async def test_update_user_disable_revokes_tokens_and_cache() -> None:
    user = _user()
    repo = FakeRepo(users=[user])
    redis = FakeRedis()
    svc, _session, _audit = _service(repo, redis=redis)
    await svc.update_user(user.id, status="disabled")
    assert repo.bumped == 1
    assert redis.deleted == [f"{PREFIX}auth:who:{user.id}"]


async def test_update_user_password_is_redacted_in_audit() -> None:
    user = _user()
    repo = FakeRepo(users=[user])
    svc, _session, audit = _service(repo)
    await svc.update_user(user.id, password="another strong password")
    diff = audit.entries[0].diff
    assert diff is not None
    assert "another strong password" not in str(diff)
    assert user.must_change_password is True


async def test_delete_user_is_soft_and_revokes() -> None:
    user = _user()
    repo = FakeRepo(users=[user])
    redis = FakeRedis()
    svc, session, audit = _service(repo, redis=redis)
    await svc.delete_user(user.id)
    assert user.deleted_at is not None
    assert user.status == "disabled"
    assert repo.bumped == 1
    assert audit.entries[0].action == "user.delete"
    assert session.commits == 1


async def test_delete_unknown_user_is_404() -> None:
    svc, _session, _audit = _service(FakeRepo())
    with pytest.raises(NotFoundError) as err:
        await svc.delete_user(uuid4())
    assert err.value.status == 404


async def test_set_member_writes_audit_and_drops_caches() -> None:
    user = _user()
    app_id = uuid4()
    repo = FakeRepo(users=[user], apps=[app_id])
    redis = FakeRedis()
    svc, session, audit = _service(repo, redis=redis)
    row = await svc.set_member(app_id, user.id, role_id="viewer")
    assert row.role_id == "viewer"
    assert repo.links[(user.id, app_id)] == "viewer"
    assert audit.entries[0].action == "member.set"
    assert audit.entries[0].app_id == app_id
    assert redis.deleted == [
        f"{PREFIX}auth:who:{user.id}",
        f"{PREFIX}auth:perm:{user.id}:{app_id}",
    ]
    assert session.commits == 1


async def test_set_member_rejects_unknown_role() -> None:
    user = _user()
    app_id = uuid4()
    svc, _session, _audit = _service(FakeRepo(users=[user], apps=[app_id]))
    with pytest.raises(ValidationAppError) as err:
        await svc.set_member(app_id, user.id, role_id="super_admin")
    assert err.value.status == 422


async def test_remove_member_is_idempotent_in_contract_but_missing_is_404() -> None:
    user = _user()
    app_id = uuid4()
    repo = FakeRepo(users=[user], apps=[app_id])
    redis = FakeRedis()
    svc, _session, audit = _service(repo, redis=redis)
    with pytest.raises(NotFoundError):
        await svc.remove_member(app_id, user.id)
    repo.links[(user.id, app_id)] = "viewer"
    await svc.remove_member(app_id, user.id)
    assert repo.links == {}
    assert audit.entries[0].action == "member.remove"
    assert f"{PREFIX}auth:perm:{user.id}:{app_id}" in redis.deleted


async def test_members_lists_users_with_roles() -> None:
    user = _user()
    app_id = uuid4()
    repo = FakeRepo(users=[user], apps=[app_id])
    repo.app_members = [(user, "viewer")]
    svc, _session, _audit = _service(repo)
    rows = await svc.members(app_id)
    assert rows[0].email == "user@example.com"
    assert rows[0].role_id == "viewer"


async def test_reset_password_returns_a_one_time_password() -> None:
    user = _user()
    repo = FakeRepo(users=[user])
    redis = FakeRedis()
    svc, session, audit = _service(repo, redis=redis)
    temporary = await svc.reset_password(user.id)
    assert len(temporary) >= 16
    assert repo.rows[0].password_hash != temporary
    assert user.must_change_password is True
    assert repo.bumped == 1
    assert audit.entries[0].action == "user.reset_password"
    assert redis.deleted == [f"{PREFIX}auth:who:{user.id}"]
    assert session.commits == 1


# ------------------------------------------------------------------ 协议层
class FakeRuntime:
    def __init__(self, service: FakeRouterService) -> None:
        self._service = service

    @asynccontextmanager
    async def sessions(self) -> AsyncIterator[None]:
        yield None

    def service(self, session: Any) -> FakeRouterService:
        return self._service

    def authorizer(self) -> Any:
        return FakeAuthorizer()


class FakeAuthorizer:
    def __init__(self, *, deny: bool = False) -> None:
        self.deny = deny

    async def authenticate(self, authorization: str | None) -> AuthedUser:
        if authorization != "Bearer good-token":
            raise UnauthenticatedError(details={"reason": "missing_token"})
        return AuthedUser(
            id=uuid4(), email="admin@example.com", display_name="管理员", is_super_admin=True
        )

    async def authorize(self, user: Any, *, app_id: Any, permission: str) -> None:
        if self.deny:
            raise ForbiddenError(details={"permission": permission})


class FakeRouterService:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def users(self, *, q: str | None = None, limit: int = 100) -> list[Any]:
        self.calls.append(("users", q, limit))
        return [SimpleNamespace(model_dump=lambda mode="json": {"email": "a@b.c"})]

    async def create_user(self, **kwargs: Any) -> Any:
        self.calls.append(("create_user", kwargs))
        return SimpleNamespace(model_dump=lambda mode="json": {"email": kwargs["email"]})

    async def update_user(self, user_id: UUID, **kwargs: Any) -> Any:
        self.calls.append(("update_user", user_id, kwargs))
        return SimpleNamespace(model_dump=lambda mode="json": {"email": "a@b.c"})

    async def delete_user(self, user_id: UUID, **kwargs: Any) -> None:
        self.calls.append(("delete_user", user_id))

    async def reset_password(self, user_id: UUID, **kwargs: Any) -> str:
        self.calls.append(("reset_password", user_id))
        return "temporary-password-1"

    async def members(self, app_id: UUID) -> list[Any]:
        self.calls.append(("members", app_id))
        return [SimpleNamespace(model_dump=lambda mode="json": {"email": "a@b.c"})]

    async def set_member(self, app_id: UUID, user_id: UUID, **kwargs: Any) -> Any:
        self.calls.append(("set_member", app_id, user_id, kwargs))
        return SimpleNamespace(model_dump=lambda mode="json": {"role_id": kwargs["role_id"]})

    async def remove_member(self, app_id: UUID, user_id: UUID, **kwargs: Any) -> None:
        self.calls.append(("remove_member", app_id, user_id))


@pytest.fixture
def client() -> tuple[TestClient, FakeRouterService]:
    service = FakeRouterService()
    app = create_app()
    app.state.auth_runtime = FakeRuntime(service)
    app.state.authorizer = FakeAuthorizer()
    return TestClient(app), service


HEADERS = {"authorization": "Bearer good-token"}


def test_users_list(client: tuple[TestClient, FakeRouterService]) -> None:
    http, service = client
    resp = http.get("/api/v1/admin/users?q=ab&limit=5", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["items"][0]["email"] == "a@b.c"
    assert service.calls[0] == ("users", "ab", 5)


def test_create_user_returns_201(client: tuple[TestClient, FakeRouterService]) -> None:
    http, service = client
    resp = http.post(
        "/api/v1/admin/users",
        json={
            "email": "new@example.com",
            "display_name": "新同事",
            "password": "correct horse battery",
        },
        headers=HEADERS,
    )
    assert resp.status_code == 201
    assert service.calls[0][0] == "create_user"


def test_create_user_rejects_short_password(client: tuple[TestClient, FakeRouterService]) -> None:
    http, _service = client
    resp = http.post(
        "/api/v1/admin/users",
        json={"email": "new@example.com", "display_name": "新同事", "password": "short"},
        headers=HEADERS,
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_patch_and_delete_user(client: tuple[TestClient, FakeRouterService]) -> None:
    http, _service = client
    user_id = uuid4()
    resp = http.patch(
        f"/api/v1/admin/users/{user_id}", json={"display_name": "改名"}, headers=HEADERS
    )
    assert resp.status_code == 200
    assert http.delete(f"/api/v1/admin/users/{user_id}", headers=HEADERS).status_code == 204


def test_members_put_and_delete(client: tuple[TestClient, FakeRouterService]) -> None:
    http, service = client
    app_id, user_id = uuid4(), uuid4()
    resp = http.put(
        f"/api/v1/admin/apps/{app_id}/members/{user_id}",
        json={"role_id": "viewer"},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["role_id"] == "viewer"
    assert (
        http.delete(f"/api/v1/admin/apps/{app_id}/members/{user_id}", headers=HEADERS).status_code
        == 204
    )
    assert http.get(f"/api/v1/admin/apps/{app_id}/members", headers=HEADERS).status_code == 200
    assert service.calls[-1][0] == "members"


def test_admin_routes_require_a_token(client: tuple[TestClient, FakeRouterService]) -> None:
    http, _service = client
    assert http.get("/api/v1/admin/users").status_code == 401


def test_reset_password_returns_the_temporary_password(
    client: tuple[TestClient, FakeRouterService],
) -> None:
    http, _service = client
    resp = http.post(f"/api/v1/admin/users/{uuid4()}/reset-password", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["password"] == "temporary-password-1"
