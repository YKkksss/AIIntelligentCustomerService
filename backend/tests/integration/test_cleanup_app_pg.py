"""M4.2 ③：应用的删除与 `aics.cleanup_app`（真 PG）。

接口侧只做三件事（置 `deleting`、吊销 Key、关闭会话、建任务），真正的数据删除在
`run_cleanup_app_job`；这里验证两段接起来以后的最终状态，以及**重复执行**是否安全
（ISSUE-76：任务挂了要能重跑）。
"""

from __future__ import annotations

import uuid
from uuid import UUID

import pytest
from sqlalchemy import select

from aics.db.models import ApiKey, App, Conversation, Feedback, Job, UserAppRole
from aics.modules.apps.admin_service import ActorMeta, AppsAdminService
from aics.modules.apps.cleanup import run_cleanup_app_job
from aics.modules.apps.service import AppService
from aics.modules.chat.repository import ConversationRepository, MessageRepository
from aics.modules.chat.schemas import NewConversation, NewMessage

pytestmark = pytest.mark.integration

META = ActorMeta(ip="10.0.0.1", user_agent="pytest", request_id="rid-cleanup")


@pytest.fixture
async def jobs(pg):
    """清理任务行会保留成平台级记录（`app_id IS NULL`），测试结束要自己删。"""
    yield
    from sqlalchemy import delete

    async with pg["sessions"]() as s:
        await s.execute(delete(Job).where(Job.app_id.is_(None), Job.created_by == pg["user_id"]))
        await s.commit()


async def _seed(pg) -> tuple[UUID, UUID, str]:
    """建一个「有数据」的应用：会话 + 消息 + Key + 反馈 + 成员关系。"""
    async with pg["sessions"]() as s:
        app = await AppService(s).create_app(name="清理验收", created_by=pg["user_id"])
        await s.flush()
        conv = await ConversationRepository(s).create(
            app.id, NewConversation(channel="api", external_user_id="u1")
        )
        msg = await MessageRepository(s).append(
            app.id,
            conv.id,
            NewMessage(role="assistant", content="答案", answer_type="answer"),
        )
        prefix = f"ak_{uuid.uuid4().hex[:10]}"
        s.add(ApiKey(app_id=app.id, name="k", prefix=prefix, key_hash="h", scopes=["chat"]))
        s.add(Feedback(app_id=app.id, message_id=msg.id, rating=1))
        s.add(UserAppRole(user_id=pg["user_id"], app_id=app.id, role_id="viewer"))
        await s.commit()
        return app.id, conv.id, prefix


async def _api_key(pg, prefix: str) -> ApiKey | None:
    async with pg["sessions"]() as s:
        return (await s.execute(select(ApiKey).where(ApiKey.prefix == prefix))).scalar_one_or_none()


async def test_delete_then_cleanup_removes_everything(pg, jobs) -> None:
    app_id, conv_id, prefix = await _seed(pg)
    calls: list[tuple[str, tuple[str, ...]]] = []

    async with pg["sessions"]() as s:
        service = AppsAdminService(s, redis=None, enqueue=lambda t, a: calls.append((t, tuple(a))))
        body = await service.delete(app_id, actor_id=pg["user_id"], meta=META)

    job_id = UUID(body["job_id"])
    assert body["revoked_keys"] == 1
    assert body["closed_conversations"] == 1
    assert calls == [("aics.cleanup_app", (str(app_id), str(job_id)))]

    async with pg["sessions"]() as s:
        app = await s.get(App, app_id)
        conv = await s.get(Conversation, conv_id)
        job = await s.get(Job, job_id)
    key = await _api_key(pg, prefix)
    assert app is not None and app.status == "deleting"
    assert conv is not None and conv.status == "closed" and conv.close_reason == "app_deleted"
    assert key is not None and key.revoked_at is not None
    assert job is not None and job.status == "queued"

    result = await run_cleanup_app_job(app_id, job_id)
    assert result["status"] == "done"
    assert result["deleted"]["conversations"] == 1
    assert result["deleted"]["messages"] == 1
    assert result["deleted"]["feedback"] == 0  # 随 messages 级联删除
    assert result["deleted"]["api_keys"] == 1

    async with pg["sessions"]() as s:
        assert await s.get(App, app_id) is None
        assert await s.get(Conversation, conv_id) is None
        kept = await s.get(Job, job_id)
    assert await _api_key(pg, prefix) is None
    assert kept is not None and kept.status == "done" and kept.app_id is None

    # 重跑：任务行已 done，直接返回，不再动数据
    again = await run_cleanup_app_job(app_id, job_id)
    assert again["status"] == "already_done"


async def test_cleanup_without_a_job_is_still_idempotent(pg, jobs) -> None:
    app_id, _conv_id, _prefix = await _seed(pg)

    first = await run_cleanup_app_job(app_id)
    second = await run_cleanup_app_job(app_id)

    assert first["status"] == "done"
    assert second["status"] == "missing"
