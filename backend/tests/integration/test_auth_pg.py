"""M4.1 ②：鉴权在真 PG + 真 Redis 上的行为（登录、刷新轮换、锁定、改密、RBAC）。

`AuthService` 的单元行为见 `tests/unit/test_auth_service.py`；这里只验证「落库之后」的语义：
`users.token_version`、`user_app_roles → roles.permissions` 联表、以及 Redis 里的刷新轮换。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest

from aics.core.config import get_settings
from aics.core.errors import (
    AccountLockedError,
    ForbiddenError,
    UnauthenticatedError,
)
from aics.core.security import AuthedUser
from aics.db.models.user import User
from aics.modules.auth import passwords, tokens
from aics.modules.auth.authorizer import AuthAuthorizer
from aics.modules.auth.repository import AuthRepository
from aics.modules.auth.service import AuthService, RequestMeta

pytestmark = pytest.mark.integration

META = RequestMeta(ip="203.0.113.7", user_agent="pytest", request_id="req-it")
PASSWORD = "correct horse battery"


@pytest.fixture
async def admin(pg: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    """A fresh super-admin account; removed afterwards (with its memberships)."""
    from sqlalchemy import delete

    from aics.db.models.app import UserAppRole

    email = f"it-auth-{uuid4().hex[:8]}@x.io"
    async with pg["sessions"]() as s:
        repo = AuthRepository(s)
        user = await repo.create_user(
            email=email, display_name="IT 管理员", password_hash=passwords.hash_password(PASSWORD)
        )
        user.must_change_password = False
        user.is_super_admin = True
        await s.commit()
        user_id = user.id
    try:
        yield {"id": user_id, "email": email, "sessions": pg["sessions"]}
    finally:
        async with pg["sessions"]() as s:
            await s.execute(delete(UserAppRole).where(UserAppRole.user_id == user_id))
            await s.execute(delete(User).where(User.id == user_id))
            await s.commit()


def _service(sessions: Any, redis: Any) -> AuthService:
    return AuthService(sessions, settings=get_settings(), redis=redis)


@pytest.fixture
async def member(pg: dict[str, Any], admin: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    """A non-super user with the `viewer` role on app_a."""
    from sqlalchemy import delete

    from aics.db.models.app import UserAppRole

    email = f"it-member-{uuid4().hex[:8]}@x.io"
    async with pg["sessions"]() as s:
        repo = AuthRepository(s)
        user = await repo.create_user(
            email=email, display_name="IT 成员", password_hash=passwords.hash_password(PASSWORD)
        )
        user.must_change_password = False
        await s.commit()
        user_id = user.id
        s.add(UserAppRole(user_id=user_id, app_id=pg["app_a"], role_id="viewer"))
        await s.commit()
    try:
        yield {"id": user_id, "email": email, "sessions": pg["sessions"]}
    finally:
        async with pg["sessions"]() as s:
            await s.execute(delete(UserAppRole).where(UserAppRole.user_id == user_id))
            await s.execute(delete(User).where(User.id == user_id))
            await s.commit()


async def test_login_me_and_rbac_over_the_database(
    pg: dict[str, Any], admin: dict[str, Any], member: dict[str, Any], redis_it: Any
) -> None:
    from aics.db.models.app import UserAppRole

    async with admin["sessions"]() as s:
        s.add(UserAppRole(user_id=admin["id"], app_id=pg["app_a"], role_id="viewer"))
        await s.commit()

    async with admin["sessions"]() as s:
        body = await _service(s, redis_it).login(admin["email"], PASSWORD, meta=META)
        assert body.user.email == admin["email"]
        assert [a.app_id for a in body.apps] == [pg["app_a"]]
        assert body.apps[0].role_id == "viewer"
        assert "log.read" in body.apps[0].permissions
        # 超级管理员在应用里也拿全量权限（角色只是展示用）
        assert "system.write" in body.apps[0].permissions

        me = await _service(s, redis_it).me(admin["id"])
        assert [a.app_id for a in me.apps] == [pg["app_a"]]

    async with member["sessions"]() as s:
        viewer = await _service(s, redis_it).login(member["email"], PASSWORD, meta=META)
        assert viewer.apps[0].role_id == "viewer"
        assert "log.read" in viewer.apps[0].permissions
        assert "kb.write" not in viewer.apps[0].permissions
        assert "system.write" not in viewer.apps[0].permissions

    authorizer = AuthAuthorizer(admin["sessions"], settings=get_settings(), redis=redis_it)
    user = await authorizer.authenticate(f"Bearer {body.access_token}")
    assert user.id == admin["id"]
    # 超级管理员虽然在应用内只有 viewer 角色，但 authorize 对 super_admin 直接放行
    await authorizer.authorize(user, app_id=pg["app_a"], permission="system.write")

    # 普通用户没有任何成员关系 → 应用内的权限一律 403（不是 404：应用存在，只是没授权）
    with pytest.raises(ForbiddenError):
        await authorizer.authorize(
            AuthedUser(id=uuid4(), email="x@y.z", display_name="x"),
            app_id=pg["app_a"],
            permission="kb.write",
        )


async def test_refresh_rotation_and_reuse_detection(
    pg: dict[str, Any], admin: dict[str, Any], redis_it: Any
) -> None:
    async with admin["sessions"]() as s:
        svc = _service(s, redis_it)
        first = await svc.login(admin["email"], PASSWORD, meta=META)
        rotated = await svc.refresh(first.refresh_token)
        assert rotated.refresh_token != first.refresh_token

    async with admin["sessions"]() as s:
        with pytest.raises(UnauthenticatedError) as err:
            await _service(s, redis_it).refresh(first.refresh_token)
        assert err.value.details["reason"] == "refresh_reused"

    async with admin["sessions"]() as s:
        row = await AuthRepository(s).user(admin["id"])
        assert row is not None and row.token_version == 1

    authorizer = AuthAuthorizer(admin["sessions"], settings=get_settings(), redis=redis_it)
    with pytest.raises(UnauthenticatedError):
        await authorizer.authenticate(f"Bearer {first.access_token}")


async def test_five_failures_lock_the_account_in_the_database(
    pg: dict[str, Any], admin: dict[str, Any], redis_it: Any
) -> None:
    for _ in range(4):
        async with admin["sessions"]() as s:
            with pytest.raises(UnauthenticatedError):
                await _service(s, redis_it).login(admin["email"], "wrong password", meta=META)
    async with admin["sessions"]() as s:
        with pytest.raises(AccountLockedError):
            await _service(s, redis_it).login(admin["email"], "wrong password", meta=META)

    async with admin["sessions"]() as s:
        row = await AuthRepository(s).user(admin["id"])
        assert row is not None
        assert row.failed_logins == 5
        assert row.locked_until is not None

    async with admin["sessions"]() as s:
        with pytest.raises(AccountLockedError):
            await _service(s, redis_it).login(admin["email"], PASSWORD, meta=META)


async def test_change_password_invalidates_the_old_access_token(
    pg: dict[str, Any], admin: dict[str, Any], redis_it: Any
) -> None:
    async with admin["sessions"]() as s:
        body = await _service(s, redis_it).login(admin["email"], PASSWORD, meta=META)
    authorizer = AuthAuthorizer(admin["sessions"], settings=get_settings(), redis=redis_it)
    assert (await authorizer.authenticate(f"Bearer {body.access_token}")).id == admin["id"]

    async with admin["sessions"]() as s:
        await _service(s, redis_it).change_password(
            admin["id"], current=PASSWORD, new="brand-new-password", meta=META
        )

    with pytest.raises(UnauthenticatedError):
        await authorizer.authenticate(f"Bearer {body.access_token}")

    async with admin["sessions"]() as s:
        row = await AuthRepository(s).user(admin["id"])
        assert row is not None
        assert passwords.verify_password("brand-new-password", row.password_hash)


async def test_tokens_from_a_deleted_user_are_rejected(
    pg: dict[str, Any], admin: dict[str, Any], redis_it: Any
) -> None:
    issued = tokens.issue(
        secret=get_settings().jwt_secret.get_secret_value(),
        user_id=uuid4(),
        token_version=0,
        kind=tokens.ACCESS,
        ttl=timedelta(seconds=60),
    )
    authorizer = AuthAuthorizer(admin["sessions"], settings=get_settings(), redis=redis_it)
    with pytest.raises(UnauthenticatedError) as err:
        await authorizer.authenticate(f"Bearer {issued.token}")
    assert err.value.details["reason"] == "unknown_user"
