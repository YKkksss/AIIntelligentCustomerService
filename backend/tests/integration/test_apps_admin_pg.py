"""M4.2 ④⑤：管理侧应用配置的乐观并发、校验路径与复制语义（真 PG）。

`PATCH` 的错误口径有三类，全部从 `AppService.update_app` / 本模块的校验冒出来：
字段越界（422 带字段路径）、版本过期（409 带 `expected` / `current`）、复制不带数据。
这里用真实数据库把这些边界钉住，接口层的契约由管理接口参考文档约束。
"""

from __future__ import annotations

import uuid
from uuid import UUID

import pytest
from sqlalchemy import func, select

from aics.core.errors import ConfigVersionConflictError, ValidationAppError
from aics.db.models import ApiKey, App, Conversation
from aics.modules.apps.admin_service import ActorMeta, AppsAdminService
from aics.modules.apps.service import AppService
from aics.modules.chat.repository import ConversationRepository
from aics.modules.chat.schemas import NewConversation

pytestmark = pytest.mark.integration

META = ActorMeta(ip="10.0.0.1", user_agent="pytest", request_id="rid-apps-admin")


async def _service(pg, session) -> AppsAdminService:
    return await AppsAdminService.load(session, settings=None)


async def _app(pg, created: list[UUID], *, name: str = "管理侧验收") -> UUID:
    async with pg["sessions"]() as s:
        app = await AppService(s).create_app(name=name, created_by=pg["user_id"])
        await s.commit()
        created.append(app.id)
        return app.id


@pytest.fixture
async def created(pg):
    """本文件自己建的应用要自己删：`apps.created_by` 外键指向 `pg` 夹具的用户。"""
    ids: list[UUID] = []
    yield ids
    from sqlalchemy import delete

    from aics.db.models import Job

    async with pg["sessions"]() as s:
        for aid in ids:
            await s.execute(delete(Conversation).where(Conversation.app_id == aid))
            await s.execute(delete(ApiKey).where(ApiKey.app_id == aid))
            await s.execute(delete(Job).where(Job.target_id == aid))
            await s.execute(delete(App).where(App.id == aid))
        await s.commit()


async def test_patch_rejects_out_of_range_with_the_field_path(pg, created) -> None:
    app_id = await _app(pg, created)

    async with pg["sessions"]() as s:
        service = await _service(pg, s)
        with pytest.raises(ValidationAppError) as err:
            await service.update(
                app_id, {"rag_config": {"fuse_top_k": 9999}}, expected_version=None, actor_id=None
            )

    errors = err.value.details["errors"]
    assert errors[0]["loc"] == ["rag_config", "fuse_top_k"]
    async with pg["sessions"]() as s:
        assert (await s.get(App, app_id)).config_version == 1


async def test_patch_with_a_stale_version_raises_conflict(pg, created) -> None:
    app_id = await _app(pg, created)

    async with pg["sessions"]() as s:
        service = await _service(pg, s)
        body = await service.update(
            app_id, {"greeting": "你好呀"}, expected_version=1, actor_id=pg["user_id"]
        )
        assert body["config_version"] == 2

    async with pg["sessions"]() as s:
        service = await _service(pg, s)
        with pytest.raises(ConfigVersionConflictError) as err:
            await service.update(
                app_id, {"greeting": "另一个"}, expected_version=1, actor_id=pg["user_id"]
            )

    assert err.value.details == {"expected": 1, "current": 2}
    async with pg["sessions"]() as s:
        assert (await s.get(App, app_id)).greeting == "你好呀"


async def test_copy_carries_the_config_but_no_data(pg, created) -> None:
    app_id = await _app(pg, created)
    async with pg["sessions"]() as s:
        service = await _service(pg, s)
        await service.update(
            app_id, {"rag_config": {"fuse_top_k": 33}}, expected_version=1, actor_id=None
        )

    async with pg["sessions"]() as s:
        conv = await ConversationRepository(s).create(
            app_id, NewConversation(channel="api", external_user_id="u-copy")
        )
        s.add(
            ApiKey(
                app_id=app_id,
                name="k",
                prefix=f"ak_{uuid.uuid4().hex[:10]}",
                key_hash="h",
                scopes=["chat"],
            )
        )
        await s.commit()
        conv_id = conv.id

    async with pg["sessions"]() as s:
        service = await _service(pg, s)
        copy = await service.copy(app_id, actor_id=pg["user_id"], meta=META)
        copy_id = UUID(copy["id"])
        created.append(copy_id)

    assert copy["name"] == "管理侧验收（副本）"
    assert copy["rag_config"]["fuse_top_k"] == 33
    assert copy["config_version"] == 1
    assert copy["stats"]["documents"] == 0

    async with pg["sessions"]() as s:
        keys = (
            await s.execute(
                select(func.count()).select_from(ApiKey).where(ApiKey.app_id == copy_id)
            )
        ).scalar_one()
        convs = (
            await s.execute(
                select(func.count()).select_from(Conversation).where(Conversation.app_id == copy_id)
            )
        ).scalar_one()
        assert (keys, convs) == (0, 0)
        # 源应用的数据没有被复制走，也没有被搬走
        assert await s.get(Conversation, conv_id) is not None


async def test_deleted_app_disappears_from_list_and_detail(pg, created) -> None:
    app_id = await _app(pg, created, name="待删除")

    async with pg["sessions"]() as s:
        service = await _service(pg, s)
        before = await service.list_apps(user_id=pg["user_id"], is_super_admin=True)
        assert str(app_id) in {item["id"] for item in before}

        removed = await service.delete(app_id, actor_id=pg["user_id"], meta=META)
        assert set(removed) == {"job_id", "revoked_keys", "closed_conversations"}

    async with pg["sessions"]() as s:
        service = await _service(pg, s)
        after = await service.list_apps(user_id=pg["user_id"], is_super_admin=True)
        assert str(app_id) not in {item["id"] for item in after}
        with pytest.raises(Exception) as err:  # noqa: B017 —— 只关心错误码与状态
            await service.detail(app_id)

    assert getattr(err.value, "code", "") == "APP_NOT_FOUND"
