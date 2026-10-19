"""M4.1 ③：审计落库与授权变更在真 PG（+真 Redis）上的语义。

三件事必须成立：审计与业务写同事务（回滚一起消失）、权限缓存被撤销后立刻失效（ISSUE-72）、
审计查询按 `id` 降序游标翻页。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import pytest

from aics.core.audit import AuditEntry
from aics.core.config import get_settings
from aics.core.errors import UnauthenticatedError
from aics.db.models.audit import AuditLog
from aics.db.models.user import User
from aics.modules.audit.repository import AuditRepository
from aics.modules.audit.service import AuditService
from aics.modules.audit.sink import PgAuditSink
from aics.modules.auth import passwords
from aics.modules.auth.authorizer import AuthAuthorizer
from aics.modules.auth.repository import AuthRepository
from aics.modules.auth.service import AuthService

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery"


@pytest.fixture
async def account(pg: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    email = f"it-audit-{uuid4().hex[:8]}@x.io"
    async with pg["sessions"]() as s:
        user = await AuthRepository(s).create_user(
            email=email, display_name="审计用户", password_hash=passwords.hash_password(PASSWORD)
        )
        user.must_change_password = False
        await s.commit()
        user_id = user.id
    try:
        yield {"id": user_id, "email": email}
    finally:
        from sqlalchemy import delete

        from aics.db.models.app import UserAppRole

        async with pg["sessions"]() as s:
            await s.execute(delete(UserAppRole).where(UserAppRole.user_id == user_id))
            await s.execute(delete(User).where(User.id == user_id))
            await s.commit()


async def test_audit_is_committed_with_the_business_write(
    pg: dict[str, Any], account: dict[str, Any]
) -> None:
    marker = f"it.audit.{uuid4().hex[:8]}"
    async with pg["sessions"]() as s:
        await PgAuditSink().write(
            s,
            AuditEntry(
                action=marker,
                actor_id=account["id"],
                target_type="user",
                target_id=account["id"],
                diff={"password": "super-secret", "display_name": "新名字"},
            ),
        )
        await s.commit()
    async with pg["sessions"]() as s:
        rows = await AuditRepository(s).page(action=marker)
    assert len(rows) == 1
    assert rows[0].diff is not None
    assert rows[0].diff["password"] == "***"
    assert rows[0].diff["display_name"] == "新名字"
    assert rows[0].actor_type == "user"


async def test_rolled_back_transaction_leaves_no_audit_row(
    pg: dict[str, Any], account: dict[str, Any]
) -> None:
    marker = f"it.audit.{uuid4().hex[:8]}"
    async with pg["sessions"]() as s:
        await PgAuditSink().write(s, AuditEntry(action=marker, actor_id=account["id"]))
        await s.rollback()
    async with pg["sessions"]() as s:
        assert await AuditRepository(s).page(action=marker) == []


async def test_audit_page_walks_backwards_with_a_cursor(pg: dict[str, Any]) -> None:
    marker = f"it.audit.{uuid4().hex[:8]}"
    async with pg["sessions"]() as s:
        for index in range(3):
            await PgAuditSink().write(s, AuditEntry(action=marker, diff={"index": index}))
        await s.commit()
    async with pg["sessions"]() as s:
        service = AuditService(s)
        first = await service.page(action=marker, page_size=2)
    assert len(first["items"]) == 2
    assert first["items"][0]["id"] > first["items"][1]["id"]
    assert first["next_cursor"] is not None
    async with pg["sessions"]() as s:
        second = await AuditService(s).page(action=marker, page_size=2, cursor=first["next_cursor"])
    assert len(second["items"]) == 1
    assert second["next_cursor"] is None


async def test_granting_a_role_takes_effect_immediately(
    pg: dict[str, Any], account: dict[str, Any], redis_it: Any
) -> None:
    from sqlalchemy import delete

    from aics.db.models.app import UserAppRole

    app_id = pg["app_a"]
    authorizer = AuthAuthorizer(pg["sessions"], settings=get_settings(), redis=redis_it)
    try:
        async with pg["sessions"]() as s:
            await s.execute(delete(UserAppRole).where(UserAppRole.user_id == account["id"]))
            await s.commit()
        assert await authorizer.permissions_of(account["id"], app_id) == frozenset()
        async with pg["sessions"]() as s:
            await AuthService(s, settings=get_settings(), redis=redis_it).set_member(
                app_id, account["id"], role_id="app_admin"
            )
        permissions = await authorizer.permissions_of(account["id"], app_id)
        assert "kb.write" in permissions
    finally:
        async with pg["sessions"]() as s:
            await s.execute(delete(UserAppRole).where(UserAppRole.user_id == account["id"]))
            await s.commit()


async def test_deleting_a_user_kills_the_access_token(
    pg: dict[str, Any], account: dict[str, Any], redis_it: Any
) -> None:
    from aics.core.security import AuthedUser

    authorizer = AuthAuthorizer(pg["sessions"], settings=get_settings(), redis=redis_it)
    async with pg["sessions"]() as s:
        service = AuthService(s, settings=get_settings(), redis=redis_it)
        issued = service._issue(
            await AuthRepository(s).user(account["id"]),
            "access",  # noqa: SLF001 - fixture shortcut
        )
    token = f"Bearer {issued.token}"
    user: AuthedUser = await authorizer.authenticate(token)
    assert user.id == account["id"]
    async with pg["sessions"]() as s:
        await AuthService(s, settings=get_settings(), redis=redis_it).delete_user(account["id"])
    with pytest.raises(UnauthenticatedError) as err:
        await authorizer.authenticate(token)
    assert err.value.details["reason"] in {"user_disabled", "unknown_user", "token_revoked"}


async def test_only_super_admin_reads_the_platform_audit(pg: dict[str, Any]) -> None:
    from aics.core.errors import ForbiddenError
    from aics.core.security import AuthedUser

    authorizer = AuthAuthorizer(pg["sessions"], settings=get_settings())
    plain = AuthedUser(id=uuid4(), email="plain@x.io", display_name="普通用户")
    with pytest.raises(ForbiddenError) as err:
        await authorizer.authorize(plain, app_id=None, permission="audit.read")
    assert err.value.details["reason"] == "super_admin_only"


async def test_audit_actions_are_distinct(pg: dict[str, Any]) -> None:
    marker = f"it.audit.{uuid4().hex[:8]}"
    async with pg["sessions"]() as s:
        await PgAuditSink().write(s, AuditEntry(action=marker))
        await PgAuditSink().write(s, AuditEntry(action=marker))
        await s.commit()
    async with pg["sessions"]() as s:
        actions = await AuditService(s).actions()
    assert actions.count(marker) == 1


async def test_audit_rows_survive_the_app(pg: dict[str, Any]) -> None:
    """`app_id` 没有外键：应用没了，审计仍要能查（数据库设计 §3.13）。"""
    marker = f"it.audit.{uuid4().hex[:8]}"
    app_id = uuid4()
    async with pg["sessions"]() as s:
        await PgAuditSink().write(s, AuditEntry(action=marker, app_id=app_id))
        await s.commit()
    async with pg["sessions"]() as s:
        rows = await s.execute(
            __import__("sqlalchemy").select(AuditLog).where(AuditLog.action == marker)
        )
        assert rows.scalars().all()[0].app_id == app_id
