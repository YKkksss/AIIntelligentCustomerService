"""M4.7 ③：公共知识库服务的集成用例（真 PG + 真审计表）。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select

from aics.core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationAppError
from aics.db.models.app import App
from aics.db.models.audit import AuditLog
from aics.db.models.knowledge import AppKBLink, KnowledgeBase
from aics.modules.audit.sink import PgAuditSink
from aics.modules.knowledge.kb_service import DEFAULT_KB_SUFFIX, KnowledgeBaseService

pytestmark = pytest.mark.integration

ACTIONS = ("kb.create", "kb.update", "kb.delete", "kb.app.link")


@pytest.fixture
async def svc(pg) -> AsyncIterator[dict[str, object]]:
    created: list[UUID] = []
    async with pg["sessions"]() as session:
        service = KnowledgeBaseService(session, audit=PgAuditSink())
        yield {"service": service, "session": session, "created": created, "pg": pg}
        await session.rollback()
    # 关联、审计、库都要清：created_by / actor_id 有外键，pg 夹具结束时要删用户
    async with pg["sessions"]() as session:
        await session.execute(delete(AppKBLink).where(AppKBLink.kb_id.in_(created)))
        await session.execute(
            delete(AuditLog).where(AuditLog.actor_id == pg["user_id"], AuditLog.action.in_(ACTIONS))
        )
        await session.execute(delete(KnowledgeBase).where(KnowledgeBase.id.in_(created)))
        await session.commit()


async def _create(svc, name: str, **kw) -> dict:
    row = await svc["service"].create(name=name, actor_id=svc["pg"]["user_id"], **kw)
    svc["created"].append(UUID(row["id"]))
    return row


async def test_create_list_and_name_conflict(svc) -> None:
    row = await _create(svc, "员工制度库", description="员工手册")
    assert row["status"] == "active" and row["config_version"] == 1
    assert row["chunk_config"]["chunk_size"] == 500

    items, total = await svc["service"].list(q="制度")
    assert total >= 1 and any(item["name"] == "员工制度库" for item in items)

    with pytest.raises(ConflictError) as excinfo:
        await _create(svc, "员工制度库")
    assert excinfo.value.code == "KNOWLEDGE_BASE_NAME_TAKEN"

    detail = await svc["service"].detail(UUID(row["id"]))
    assert detail["document_count"] == 0 and detail["apps"] == []


async def test_update_bumps_version_only_for_chunk_config(svc) -> None:
    row = await _create(svc, "产品库")
    kb_id = UUID(row["id"])

    renamed = await svc["service"].update(
        kb_id, {"description": "产品资料"}, actor_id=svc["pg"]["user_id"]
    )
    assert renamed["description"] == "产品资料" and renamed["config_version"] == 1

    resized = await svc["service"].update(
        kb_id, {"chunk_config": {"chunk_size": 300}}, actor_id=svc["pg"]["user_id"]
    )
    assert resized["config_version"] == 2 and resized["chunk_config"]["chunk_size"] == 300

    with pytest.raises(ValidationAppError) as excinfo:
        await svc["service"].update(
            kb_id, {"chunk_config": {"chunk_size": 20}}, actor_id=svc["pg"]["user_id"]
        )
    errors = excinfo.value.details["errors"]
    assert errors[0]["loc"][:2] == ["chunk_config", "chunk_size"]

    with pytest.raises(ValidationAppError):
        await svc["service"].update(kb_id, {"unknown": 1}, actor_id=svc["pg"]["user_id"])


async def test_links_and_default_kb(svc) -> None:
    pg = svc["pg"]
    first = await _create(svc, "测试 制度库")
    second = await _create(svc, "产品库")

    linked = await svc["service"].set_links(
        pg["app_a"], [UUID(second["id"]), UUID(first["id"])], actor_id=pg["user_id"]
    )
    assert {item["id"] for item in linked} == {second["id"], first["id"]}
    assert all(item["linked"] for item in linked)
    # 没指定默认时取本次列表的第一条
    assert [item["id"] for item in linked if item["is_default"]] == [second["id"]]
    assert await svc["service"].default_kb_of(pg["app_a"]) == UUID(second["id"])

    # 显式换默认，并且后续不带 default 的重放不会把默认改回去
    switched = await svc["service"].set_links(
        pg["app_a"],
        [UUID(second["id"]), UUID(first["id"])],
        default_kb_id=UUID(first["id"]),
        actor_id=pg["user_id"],
    )
    assert [item["id"] for item in switched if item["is_default"]] == [first["id"]]
    replayed = await svc["service"].set_links(pg["app_a"], [UUID(second["id"]), UUID(first["id"])])
    assert [item["id"] for item in replayed if item["is_default"]] == [first["id"]]

    rows = await svc["session"].execute(
        select(AuditLog.action).where(AuditLog.actor_id == pg["user_id"])
    )
    actions = set(rows.scalars())
    assert {"kb.create", "kb.app.link"} <= actions

    # 参数里出现不存在的库 → 404
    with pytest.raises(NotFoundError) as excinfo:
        await svc["service"].set_links(pg["app_a"], [uuid4()], actor_id=pg["user_id"])
    assert excinfo.value.code == "KNOWLEDGE_BASE_NOT_FOUND"


async def test_delete_requires_unlink_then_soft_deletes(svc) -> None:
    pg = svc["pg"]
    row = await _create(svc, "待删库")
    kb_id = UUID(row["id"])
    await svc["service"].set_links(pg["app_a"], [kb_id], actor_id=pg["user_id"])

    with pytest.raises(ConflictError) as excinfo:
        await svc["service"].delete(kb_id, actor_id=pg["user_id"])
    assert excinfo.value.code == "KNOWLEDGE_BASE_IN_USE"

    await svc["service"].set_links(pg["app_a"], [], actor_id=pg["user_id"])
    await svc["service"].delete(kb_id, actor_id=pg["user_id"])

    with pytest.raises(NotFoundError):
        await svc["service"].detail(kb_id)


async def test_default_kb_falls_back_to_migrated_name(pg) -> None:
    """未关联任何库时，按「<应用名> 默认知识库」找迁移期回填的库。"""
    async with pg["sessions"]() as session:
        service = KnowledgeBaseService(session, audit=PgAuditSink())
        app = await session.get(App, pg["app_b"])
        assert app is not None
        row = await service.create(name=f"{app.name} {DEFAULT_KB_SUFFIX}", actor_id=pg["user_id"])
        await session.commit()
    try:
        async with pg["sessions"]() as session:
            service = KnowledgeBaseService(session, audit=PgAuditSink())
            assert await service.default_kb_of(pg["app_b"]) == UUID(row["id"])
    finally:
        async with pg["sessions"]() as session:
            await session.execute(delete(KnowledgeBase).where(KnowledgeBase.id == UUID(row["id"])))
            await session.execute(
                delete(AuditLog).where(
                    AuditLog.actor_id == pg["user_id"], AuditLog.action.in_(ACTIONS)
                )
            )
            await session.commit()


async def test_documents_lists_with_app_name(svc) -> None:
    """公共库页的「库内文档」：带 app_name，按创建时间倒序，总量正确（M4.7 ⑤）。"""
    from aics.db.models.knowledge import Document

    kb = await _create(svc, f"库内文档-{uuid4().hex[:6]}")
    kb_id = UUID(kb["id"])
    app_id = svc["pg"]["app_a"]
    session = svc["session"]
    for index in range(2):
        doc = Document(
            app_id=app_id,
            kb_id=kb_id,
            title=f"文档 {index}",
            source_type="file",
            content_hash=uuid4().hex * 2,
            status="done",
            created_by=svc["pg"]["user_id"],
        )
        session.add(doc)
    await session.commit()

    items, total = await svc["service"].documents(kb_id)
    app_name = (await session.execute(select(App.name).where(App.id == app_id))).scalar_one()
    await session.execute(delete(Document).where(Document.kb_id == kb_id))
    await session.commit()

    assert total == 2
    assert [item["title"] for item in items] == ["文档 1", "文档 0"]
    assert all(item["app_name"] == app_name for item in items)
    assert all(item["status"] == "done" for item in items)


async def test_member_cannot_manage_a_kb_they_do_not_own(svc) -> None:
    """公共库的写操作只认创建者与超管；普通成员只能用，不能改。"""
    service = svc["service"]
    owner = svc["pg"]["user_id"]
    other = uuid4()
    kb = await _create(svc, f"权限口径库-{uuid4().hex[:6]}")
    with pytest.raises(ForbiddenError):
        await service.update(kb["id"], {"description": "改一下"}, actor_id=other)
    with pytest.raises(ForbiddenError):
        await service.delete(kb["id"], actor_id=other)
    # 超管可以代管
    updated = await service.update(
        kb["id"], {"description": "超管改的"}, actor_id=other, is_super_admin=True
    )
    assert updated["description"] == "超管改的"
