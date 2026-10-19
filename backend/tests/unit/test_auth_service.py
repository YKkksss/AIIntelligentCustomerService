"""M4.1 ②：`AuthService` 的登录 / 刷新轮换 / 锁定 / 改密（假 repository + 假 Redis）。

真库上的行为（`token_version`、`user_app_roles` 联表）由 `tests/integration/test_auth_pg.py` 覆盖。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from aics.core.config import get_settings
from aics.core.errors import (
    AccountLockedError,
    UnauthenticatedError,
    ValidationAppError,
)
from aics.modules.auth import passwords, tokens
from aics.modules.auth import service as auth_service
from aics.modules.auth.service import AuthService, RequestMeta

META = RequestMeta(ip="203.0.113.9", user_agent="pytest", request_id="req-1")


class FakePipeline:
    def __init__(self, owner: FakeRedis) -> None:
        self.owner = owner
        self.ops: list[tuple[Any, ...]] = []

    async def __aenter__(self) -> FakePipeline:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def incr(self, key: str) -> None:
        self.ops.append(("incr", key))

    def expire(self, key: str, ttl: int) -> None:
        self.ops.append(("expire", key, ttl))

    async def execute(self) -> list[Any]:
        count = 0
        for op in self.ops:
            if op[0] == "incr":
                count = self.owner.counters.get(op[1], 0) + 1
                self.owner.counters[op[1]] = count
        return [count, True]


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.counters: dict[str, int] = {}

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        return FakePipeline(self)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.data[key] = value

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


def _user(**over: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "id": uuid4(),
        "email": "admin@example.com",
        "display_name": "管理员",
        "password_hash": passwords.hash_password("correct horse battery"),
        "is_super_admin": False,
        "status": "active",
        "failed_logins": 0,
        "locked_until": None,
        "last_login_at": None,
        "must_change_password": False,
        "token_version": 0,
    }
    values.update(over)
    return SimpleNamespace(**values)


class FakeRepo:
    def __init__(self, row: SimpleNamespace | None) -> None:
        self.row = row
        self.members: list[tuple[Any, Any, Any]] = []
        self.bumped = 0

    async def user_by_email(self, email: str) -> Any:
        return self.row if self.row is not None and self.row.email == email.lower() else None

    async def user(self, user_id: Any) -> Any:
        return self.row if self.row is not None and self.row.id == user_id else None

    async def memberships(self, user_id: Any) -> list[Any]:
        return self.members

    async def record_failure(self, user: Any, *, at: datetime, lock_minutes: int) -> int:
        user.failed_logins = (user.failed_logins or 0) + 1
        if user.failed_logins >= 5:
            user.locked_until = at + timedelta(minutes=lock_minutes)
        return user.failed_logins

    async def bump_token_version(self, user: Any) -> int:
        self.bumped += 1
        user.token_version = (user.token_version or 0) + 1
        return user.token_version


def _svc(
    monkeypatch: pytest.MonkeyPatch, user: Any, *, redis: FakeRedis | None = None
) -> tuple[AuthService, FakeRepo, FakeSession, FakeRedis]:
    repo = FakeRepo(user)
    monkeypatch.setattr(auth_service, "AuthRepository", lambda session: repo)
    session = FakeSession()
    cache = redis if redis is not None else FakeRedis()
    svc = AuthService(session, settings=get_settings(), redis=cache)  # type: ignore[arg-type]
    return svc, repo, session, cache


async def test_login_success_returns_tokens_and_stores_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _user()
    svc, _repo, session, cache = _svc(monkeypatch, user)
    body = await svc.login("Admin@Example.com", "correct horse battery", meta=META)
    assert body.user.id == user.id
    assert body.expires_in == 30 * 60
    payload = tokens.decode(
        secret=get_settings().jwt_secret.get_secret_value(),
        token=body.access_token,
        kind=tokens.ACCESS,
    )
    assert payload.user_id == user.id
    refresh = tokens.decode(
        secret=get_settings().jwt_secret.get_secret_value(),
        token=body.refresh_token,
        kind=tokens.REFRESH,
    )
    assert cache.data[f"{get_settings().redis_prefix}auth:refresh:{refresh.jti}"]
    assert session.commits == 1
    assert user.failed_logins == 0


async def test_login_unknown_email_looks_like_wrong_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc, _repo, session, _cache = _svc(monkeypatch, None)
    with pytest.raises(UnauthenticatedError) as err:
        await svc.login("nobody@example.com", "correct horse battery", meta=META)
    assert err.value.code == "UNAUTHENTICATED"
    assert err.value.message == "邮箱或密码不正确"
    assert session.commits == 0


async def test_login_disabled_user_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    svc, _repo, _session, _cache = _svc(monkeypatch, _user(status="disabled"))
    with pytest.raises(UnauthenticatedError):
        await svc.login("admin@example.com", "correct horse battery", meta=META)


async def test_five_failures_lock_the_account(monkeypatch: pytest.MonkeyPatch) -> None:
    user = _user()
    svc, _repo, session, _cache = _svc(monkeypatch, user)
    for attempt in range(1, 5):
        with pytest.raises(UnauthenticatedError):
            await svc.login("admin@example.com", "wrong password", meta=META)
        assert user.failed_logins == attempt
    with pytest.raises(AccountLockedError) as err:
        await svc.login("admin@example.com", "wrong password", meta=META)
    assert err.value.status == 423
    assert int(err.value.headers["Retry-After"]) > 0
    assert user.locked_until is not None
    assert session.commits == 5


async def test_locked_account_rejects_before_checking_the_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locked = datetime.now(UTC) + timedelta(minutes=10)
    svc, _repo, _session, _cache = _svc(monkeypatch, _user(locked_until=locked))
    with pytest.raises(AccountLockedError):
        await svc.login("admin@example.com", "correct horse battery", meta=META)


async def test_refresh_rotates_the_token(monkeypatch: pytest.MonkeyPatch) -> None:
    user = _user()
    svc, _repo, _session, cache = _svc(monkeypatch, user)
    body = await svc.login("admin@example.com", "correct horse battery", meta=META)
    rotated = await svc.refresh(body.refresh_token)
    assert rotated.refresh_token != body.refresh_token
    old = tokens.decode(
        secret=get_settings().jwt_secret.get_secret_value(),
        token=body.refresh_token,
        kind=tokens.REFRESH,
    )
    assert f"{get_settings().redis_prefix}auth:refresh:{old.jti}" not in cache.data


async def test_refresh_reuse_revokes_every_session(monkeypatch: pytest.MonkeyPatch) -> None:
    user = _user()
    svc, repo, _session, _cache = _svc(monkeypatch, user)
    body = await svc.login("admin@example.com", "correct horse battery", meta=META)
    await svc.refresh(body.refresh_token)
    with pytest.raises(UnauthenticatedError) as err:
        await svc.refresh(body.refresh_token)
    assert err.value.details["reason"] == "refresh_reused"
    assert repo.bumped == 1


async def test_refresh_with_stale_token_version_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _user()
    svc, _repo, _session, _cache = _svc(monkeypatch, user)
    body = await svc.login("admin@example.com", "correct horse battery", meta=META)
    user.token_version = 7
    with pytest.raises(UnauthenticatedError) as err:
        await svc.refresh(body.refresh_token)
    assert err.value.details["reason"] == "token_revoked"


async def test_change_password_checks_the_current_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _user()
    svc, _repo, _session, _cache = _svc(monkeypatch, user)
    with pytest.raises(ValidationAppError):
        await svc.change_password(user.id, current="nope", new="new-password", meta=META)
    with pytest.raises(ValidationAppError):
        await svc.change_password(
            user.id, current="correct horse battery", new="correct horse battery", meta=META
        )


async def test_change_password_bumps_version_and_clears_the_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _user(must_change_password=True)
    svc, _repo, session, _cache = _svc(monkeypatch, user)
    body = await svc.change_password(
        user.id, current="correct horse battery", new="brand-new-password", meta=META
    )
    assert user.must_change_password is False
    assert user.token_version == 1
    assert passwords.verify_password("brand-new-password", user.password_hash)
    payload = tokens.decode(
        secret=get_settings().jwt_secret.get_secret_value(),
        token=body.access_token,
        kind=tokens.ACCESS,
    )
    assert payload.token_version == 1
    assert session.commits == 1


async def test_me_reports_memberships_and_super_admin_permissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _user(is_super_admin=True)
    svc, repo, _session, _cache = _svc(monkeypatch, user)
    repo.members = [
        (
            SimpleNamespace(role_id="viewer"),
            SimpleNamespace(id=uuid4(), name="客服A"),
            SimpleNamespace(permissions=["app.read"]),
        )
    ]
    body = await svc.me(user.id)
    assert body.user.email == "admin@example.com"
    assert body.apps[0].app_name == "客服A"
    assert body.apps[0].role_id == "viewer"
    assert len(body.apps[0].permissions) == 25  # 超级管理员在应用内拿到全部权限


async def test_logout_is_forgiving(monkeypatch: pytest.MonkeyPatch) -> None:
    user = _user()
    svc, _repo, _session, cache = _svc(monkeypatch, user)
    body = await svc.login("admin@example.com", "correct horse battery", meta=META)
    await svc.logout(body.refresh_token)
    assert not [k for k in cache.data if "auth:refresh:" in k]
    await svc.logout("garbage")  # 过期 / 伪造的 token 不抛
    await svc.logout(None)
