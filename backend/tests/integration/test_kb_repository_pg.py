"""知识库与关联表的集成用例（M4.7 ②）。

只验证 ② 的数据层语义：库的增改查、关联的全量替换、按库查应用、分页与计数。
`created_by` 有外键（`fk_knowledge_bases_created_by_users`），`pg` 夹具会在结束时删用户，
所以这里造出来的库必须在用例内清掉，否则夹具清理会失败。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

import pytest

from aics.db.models.knowledge import KnowledgeBase
from aics.modules.knowledge.kb_repository import (
    AppKBLinkRepository,
    KnowledgeBaseRepository,
    kb_row,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def kbs(pg) -> AsyncIterator[dict[str, object]]:
    created: list[UUID] = []
    async with pg["sessions"]() as s:
        repo = KnowledgeBaseRepository(s)
        first = await repo.create(KnowledgeBase(name="制度库", description="员工手册与制度"))
        second = await repo.create(KnowledgeBase(name="产品库", chunk_config={"chunk_size": 400}))
        created.extend([first.id, second.id])
        await s.commit()
    yield {"sessions": pg["sessions"], "ids": created, "first": created[0], "second": created[1]}
    async with pg["sessions"]() as s:
        await AppKBLinkRepository(s).replace(pg["app_a"], [])
        await AppKBLinkRepository(s).replace(pg["app_b"], [])
        for kb_id in created:
            await KnowledgeBaseRepository(s).save(kb_id, status="deleting")
        await s.execute(KnowledgeBase.__table__.delete().where(KnowledgeBase.id.in_(created)))
        await s.commit()


async def test_list_and_count_filter_by_status(kbs) -> None:
    async with kbs["sessions"]() as s:
        repo = KnowledgeBaseRepository(s)
        rows = await repo.list(limit=50)
        assert {r.name for r in rows} >= {"制度库", "产品库"}
        assert await repo.count() >= 2
        assert all(r.status == "active" for r in rows)


async def test_row_view_has_no_internal_fields(kbs) -> None:
    async with kbs["sessions"]() as s:
        kb = await KnowledgeBaseRepository(s).get(kbs["first"])
    assert kb is not None
    row = kb_row(kb, documents=3, chunks=9)
    assert row["document_count"] == 3 and row["chunk_count"] == 9
    assert row["config_version"] == 1
    # 前端要按 created_by 判断「我是不是这个库的管理者」，所以视图里保留它
    assert row["created_by"] == (str(kb.created_by) if kb.created_by else None)


async def test_replace_reports_added_and_removed(kbs, pg) -> None:
    async with kbs["sessions"]() as s:
        links = AppKBLinkRepository(s)
        added, removed = await links.replace(pg["app_a"], [kbs["first"]])
        assert added == [kbs["first"]] and removed == []
        added, removed = await links.replace(pg["app_a"], [kbs["second"]], created_by=pg["user_id"])
        assert added == [kbs["second"]] and removed == [kbs["first"]]
        assert await links.kb_ids(pg["app_a"]) == [kbs["second"]]
        # 全量替换是幂等的：同样的集合不再产生变更
        added, removed = await links.replace(pg["app_a"], [kbs["second"]])
        assert added == [] and removed == []


async def test_links_are_per_app(kbs, pg) -> None:
    async with kbs["sessions"]() as s:
        links = AppKBLinkRepository(s)
        await links.replace(pg["app_a"], [kbs["first"]])
        await links.replace(pg["app_b"], [kbs["second"]])
        assert await links.kb_ids(pg["app_a"]) == [kbs["first"]]
        assert await links.kb_ids(pg["app_b"]) == [kbs["second"]]
        assert set(await links.apps_of_kb_all_apps(kbs["first"])) == {pg["app_a"]}
