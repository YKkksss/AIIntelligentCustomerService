"""M4.5 ②：Key 管理的真库语义——明文只出现一次、吊销/改限额立刻生效、跨应用 404。

缓存失效是这里的主角：`ApiKeyAuthenticator` 把 Key 记录写在 Redis 里，
管理侧改完必须删掉，否则吊销要等 TTL 才生效（安全设计 §2）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from aics.core.audit import AuditSink
from aics.core.config import get_settings
from aics.core.errors import NotFoundError
from aics.db.models import ApiKey
from aics.db.models.audit import AuditLog
from aics.modules.apikeys.admin_service import ApiKeyAdminService
from aics.modules.apikeys.service import ApiKeyAuthenticator, ApiKeyRevokedError, IpNotAllowedError
from aics.modules.audit.sink import PgAuditSink

pytestmark = pytest.mark.integration

SCOPES = ["chat"]


@asynccontextmanager
async def admin_for(
    pg: dict[str, Any], redis: Any = None, *, audit: AuditSink | None = None
) -> AsyncIterator[ApiKeyAdminService]:
    async with pg["sessions"]() as s:
        yield ApiKeyAdminService(
            s,
            pepper=get_settings().api_key_pepper.get_secret_value(),
            redis=redis,
            audit=audit,
        )


async def authenticator(pg: dict[str, Any], redis: Any) -> ApiKeyAuthenticator:
    return ApiKeyAuthenticator(
        pg["sessions"],
        pepper=get_settings().api_key_pepper.get_secret_value(),
        redis=redis,
    )


async def test_plaintext_is_shown_once_and_only_a_hash_is_stored(pg: dict[str, Any]) -> None:
    app_id = pg["app_a"]
    async with admin_for(pg) as admin:
        created = await admin.create_key(app_id, name="上线", scopes=SCOPES)
        plain = created["key"]
        assert plain.startswith("aics_")

        listed = await admin.list_keys(app_id)
        assert [item["id"] for item in listed] == [created["id"]]
        assert "key" not in listed[0]
        assert plain not in str(listed)

    async with pg["sessions"]() as s:
        row = (await s.scalars(select(ApiKey).where(ApiKey.app_id == app_id))).one()
    assert row.key_hash != plain
    assert plain not in row.key_hash


async def test_revoke_kills_the_cached_record_immediately(
    pg: dict[str, Any], redis_it: Any
) -> None:
    app_id = pg["app_a"]
    async with admin_for(pg, redis_it) as admin:
        created = await admin.create_key(app_id, name="上线", scopes=SCOPES, rpm_limit=100)
    auth = await authenticator(pg, redis_it)

    # 先认证一次，把 Key 记录写进 Redis 缓存
    principal = await auth.authenticate(
        f"Bearer {created['key']}", app_id=app_id, any_scope=("chat",)
    )
    assert principal.key_prefix == created["prefix"]

    async with admin_for(pg, redis_it) as admin:
        await admin.revoke_key(app_id, created["id"])

    with pytest.raises(ApiKeyRevokedError):
        await auth.authenticate(f"Bearer {created['key']}", app_id=app_id, any_scope=("chat",))


async def test_limits_are_visible_after_update(pg: dict[str, Any], redis_it: Any) -> None:
    app_id = pg["app_a"]
    async with admin_for(pg, redis_it) as admin:
        created = await admin.create_key(app_id, name="上线", scopes=SCOPES, rpm_limit=10)
        await admin.update_key(app_id, created["id"], rpm_limit=3, daily_quota=99)

        listed = await admin.list_keys(app_id)
    assert listed[0]["rpm_limit"] == 3
    assert listed[0]["daily_quota"] == 99


async def test_tightened_allowlist_takes_effect_on_the_next_request(
    pg: dict[str, Any], redis_it: Any
) -> None:
    app_id = pg["app_a"]
    async with admin_for(pg, redis_it) as admin:
        created = await admin.create_key(app_id, name="上线", scopes=SCOPES)
    auth = await authenticator(pg, redis_it)

    await auth.authenticate(
        f"Bearer {created['key']}", app_id=app_id, any_scope=("chat",), client_ip="10.1.2.3"
    )

    async with admin_for(pg, redis_it) as admin:
        await admin.update_key(app_id, created["id"], ip_allowlist=["10.1.2.3/32"])

    with pytest.raises(IpNotAllowedError):
        await auth.authenticate(
            f"Bearer {created['key']}", app_id=app_id, any_scope=("chat",), client_ip="10.9.9.9"
        )


async def test_unknown_key_is_404_with_the_key_id(pg: dict[str, Any]) -> None:
    app_id = pg["app_a"]
    missing = uuid4()
    async with admin_for(pg) as admin:
        with pytest.raises(NotFoundError) as info:
            await admin.update_key(app_id, missing, rpm_limit=1)
    assert info.value.details == {"key_id": str(missing)}


async def test_a_key_of_another_app_is_404(pg: dict[str, Any]) -> None:
    app_a, app_b = pg["app_a"], pg["app_b"]
    async with admin_for(pg) as admin:
        created = await admin.create_key(app_a, name="A", scopes=SCOPES)
        with pytest.raises(NotFoundError):
            await admin.revoke_key(app_b, created["id"])


async def test_audit_rows_land_in_the_same_transaction(pg: dict[str, Any]) -> None:
    app_id = pg["app_a"]
    async with admin_for(pg, audit=PgAuditSink()) as admin:
        created = await admin.create_key(app_id, name="上线", scopes=SCOPES, actor_id=pg["user_id"])
        await admin.update_key(app_id, created["id"], rpm_limit=7, actor_id=pg["user_id"])
        await admin.revoke_key(app_id, created["id"], actor_id=pg["user_id"])

    async with pg["sessions"]() as s:
        rows = (
            await s.scalars(
                select(AuditLog)
                .where(AuditLog.app_id == app_id, AuditLog.target_id == created["id"])
                .order_by(AuditLog.id)
            )
        ).all()
    assert [row.action for row in rows] == ["key.create", "key.update", "key.revoke"]
    assert all(row.target_type == "api_key" for row in rows)
    for row in rows:
        assert "key" not in (row.diff or {}).get("after", {})
    # 审计行的 actor_id 外键指向 users，共用夹具删用户前先清掉
    async with pg["sessions"]() as s:
        await s.execute(delete(AuditLog).where(AuditLog.app_id == app_id))
        await s.commit()


async def test_expired_key_reports_expired_status(pg: dict[str, Any]) -> None:
    app_id = pg["app_a"]
    async with admin_for(pg) as admin:
        created = await admin.create_key(
            app_id, name="旧的", scopes=SCOPES, expires_at=datetime.now(UTC) - timedelta(minutes=1)
        )
        listed = await admin.list_keys(app_id)
    assert listed[0]["id"] == created["id"]
    assert listed[0]["status"] == "expired"
