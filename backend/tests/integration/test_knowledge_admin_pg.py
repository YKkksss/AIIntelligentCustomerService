"""M4.3 ②：管理版知识库接口的集成用例（真 PG + 本地存储 + 假向量库/假嵌入器）。

只覆盖单测看不到的部分：写操作的审计与业务同事务、`chunk_config` 的解析与错误路径、
切块预览不落库、任务列表、权限拒绝。真正的解析/切块/入库仍由入库流水线用例覆盖。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from ingest_support import FakeEmbedder
from sqlalchemy import func, select, update

from aics.core.audit import AuditEntry
from aics.core.errors import ForbiddenError, UnauthenticatedError
from aics.core.security import AuthedUser
from aics.db.models import App, AuditLog, Chunk, Document
from aics.main import create_app
from aics.modules.audit.sink import PgAuditSink
from aics.modules.knowledge.runtime import KnowledgeRuntime
from aics.modules.knowledge.service import KnowledgeService

pytestmark = pytest.mark.integration

MD = "# 退货政策\n\n第 1 节 7 天内可退回，运费由我们承担。\n\n第 2 节 请保留原包装。\n".encode()
MD_LONG = (
    "# 退货政策\n\n"
    + "\n\n".join(
        f"第 {i} 条 自签收之日起七天内可无理由退货，运费由我们承担；请保留原包装与配件。"
        for i in range(1, 40)
    )
).encode()


def _file(filename: str = "退货政策.md", data: bytes = MD) -> dict[str, Any]:
    return {"file": (filename, data, "text/markdown")}


class FakeAuthorizer:
    def __init__(self, deny: str | None = None, *, user_id: uuid.UUID) -> None:
        self.deny = deny
        self.user_id = user_id
        self.actor = AuthedUser(
            id=self.user_id, email="admin@example.com", display_name="管理员", is_super_admin=True
        )

    async def authenticate(self, authorization: str | None) -> AuthedUser:
        if authorization != "Bearer admin":
            raise UnauthenticatedError(details={"reason": "missing_token"})
        return self.actor

    async def authorize(self, user: Any, *, app_id: Any, permission: str) -> None:
        if self.deny is not None and permission == self.deny:
            raise ForbiddenError(details={"reason": "missing_permission", "permission": permission})


@pytest.fixture
async def admin(env: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    sent: list[tuple[str, list[Any]]] = []
    async with env["sessions"]() as s:
        owner = await s.scalar(select(App.created_by).where(App.id == env["app_id"]))
    authorizer = FakeAuthorizer(user_id=owner)
    app = create_app()
    app.state.authorizer = authorizer
    app.state.knowledge_runtime = KnowledgeRuntime(
        sessions=env["sessions"],
        storage=env["storage"],
        vector=env["store"],
        embedder=FakeEmbedder(),
        enqueue=lambda task, args: sent.append((task, list(args))),
        audit=PgAuditSink(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t", timeout=30) as client:
        yield {
            "client": client,
            "app_id": env["app_id"],
            "env": env,
            "sent": sent,
            "user_id": authorizer.user_id,
            "authorizer": authorizer,
        }


def auth() -> dict[str, str]:
    return {"authorization": "Bearer admin"}


async def _audit_rows(env: dict[str, Any], action: str) -> list[AuditLog]:
    async with env["sessions"]() as s:
        rows = await s.scalars(
            select(AuditLog).where(AuditLog.app_id == env["app_id"], AuditLog.action == action)
        )
        return list(rows)


async def _mark_done(env: dict[str, Any], document_id: str) -> None:
    async with env["sessions"]() as s:
        await s.execute(
            update(Document)
            .where(Document.app_id == env["app_id"], Document.id == uuid.UUID(document_id))
            .values(status="done", progress=100, index_version=1, chunk_count=2)
        )
        await s.commit()


async def test_upload_writes_document_and_audit_in_the_same_transaction(
    admin: dict[str, Any],
) -> None:
    r = await admin["client"].post(
        f"/api/v1/admin/apps/{admin['app_id']}/documents",
        headers=auth(),
        files=_file(),
        data={"title": "退货政策", "chunk_config": '{"chunk_size": 400}'},
    )

    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "queued" and body["job_id"]
    assert admin["sent"][0][0] == "aics.ingest_document"

    async with admin["env"]["sessions"]() as s:
        doc = await s.get(Document, uuid.UUID(body["id"]))
        assert doc is not None
        assert doc.title == "退货政策"
        assert doc.chunk_config["chunk_size"] == 400
        assert doc.chunk_config["chunk_overlap"] is not None  # 合并后是完整快照
    rows = await _audit_rows(admin["env"], "kb.document.create")
    assert len(rows) == 1
    assert rows[0].actor_id == admin["user_id"]
    assert rows[0].target_id == uuid.UUID(body["id"])


async def test_upload_with_invalid_chunk_config_reports_the_path(admin: dict[str, Any]) -> None:
    r = await admin["client"].post(
        f"/api/v1/admin/apps/{admin['app_id']}/documents",
        headers=auth(),
        files=_file(),
        data={"chunk_config": '{"chunk_size": 1}'},
    )

    assert r.status_code == 422, r.text
    loc = r.json()["error"]["details"]["errors"][0]["loc"]
    assert loc[:2] == ["chunk_config", "chunk_size"]
    assert await _audit_rows(admin["env"], "kb.document.create") == []


async def test_preview_upload_returns_chunks_without_writing(admin: dict[str, Any]) -> None:
    async with admin["env"]["sessions"]() as s:
        before = await s.scalar(
            select(func.count()).select_from(Document).where(Document.app_id == admin["app_id"])
        )

    r = await admin["client"].post(
        f"/api/v1/admin/apps/{admin['app_id']}/documents/preview",
        headers=auth(),
        files=_file(),
        data={"chunk_config": '{"chunk_size": 300}'},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_chunks"] >= 1
    assert body["chunks"][0]["text"]
    async with admin["env"]["sessions"]() as s:
        after = await s.scalar(
            select(func.count()).select_from(Document).where(Document.app_id == admin["app_id"])
        )
    assert after == before


async def test_preview_document_applies_the_override_config(admin: dict[str, Any]) -> None:
    created = await admin["client"].post(
        f"/api/v1/admin/apps/{admin['app_id']}/documents",
        headers=auth(),
        files=_file(data=MD_LONG),
    )
    doc_id = created.json()["id"]
    await _mark_done(admin["env"], doc_id)

    small = await admin["client"].post(
        f"/api/v1/admin/apps/{admin['app_id']}/documents/{doc_id}/preview",
        headers=auth(),
        json={"chunk_config": {"chunk_size": 200, "overlap": 20}},
    )
    large = await admin["client"].post(
        f"/api/v1/admin/apps/{admin['app_id']}/documents/{doc_id}/preview",
        headers=auth(),
        json={"chunk_config": {"chunk_size": 2000, "overlap": 20}},
    )

    assert small.status_code == 200, small.text
    assert large.status_code == 200, large.text
    assert small.json()["total_chunks"] > large.json()["total_chunks"]


async def test_jobs_endpoint_lists_the_ingest_job(admin: dict[str, Any]) -> None:
    await admin["client"].post(
        f"/api/v1/admin/apps/{admin['app_id']}/documents", headers=auth(), files=_file()
    )

    r = await admin["client"].get(
        f"/api/v1/admin/apps/{admin['app_id']}/jobs?type=ingest", headers=auth()
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] >= 1
    assert body["items"][0]["type"] == "ingest"
    assert body["items"][0]["created_at"]


async def test_delete_marks_deleting_and_writes_audit(admin: dict[str, Any]) -> None:
    created = await admin["client"].post(
        f"/api/v1/admin/apps/{admin['app_id']}/documents", headers=auth(), files=_file()
    )
    doc_id = created.json()["id"]

    r = await admin["client"].delete(
        f"/api/v1/admin/apps/{admin['app_id']}/documents/{doc_id}", headers=auth()
    )

    assert r.status_code == 202, r.text
    async with admin["env"]["sessions"]() as s:
        doc = await s.get(Document, uuid.UUID(doc_id))
        assert doc is not None and doc.status == "deleting" and doc.deleted_at is not None
    rows = await _audit_rows(admin["env"], "kb.document.delete")
    assert len(rows) == 1 and rows[0].target_id == uuid.UUID(doc_id)


async def test_read_only_member_cannot_upload(admin: dict[str, Any]) -> None:
    admin["authorizer"].deny = "kb.write"

    r = await admin["client"].post(
        f"/api/v1/admin/apps/{admin['app_id']}/documents", headers=auth(), files=_file()
    )

    assert r.status_code == 403


async def test_upload_without_token_is_401(admin: dict[str, Any]) -> None:
    r = await admin["client"].post(
        f"/api/v1/admin/apps/{admin['app_id']}/documents",
        files=_file(),
        headers={"authorization": "Bearer nope"},
    )

    assert r.status_code == 401


class _FailingSink:
    async def write(self, session: Any, entry: AuditEntry) -> None:
        raise RuntimeError("audit down")


async def test_audit_failure_rolls_back_the_document(env: dict[str, Any]) -> None:
    async with env["sessions"]() as s:
        owner = await s.scalar(select(App.created_by).where(App.id == env["app_id"]))
        service = KnowledgeService(s, storage=env["storage"], audit=_FailingSink())
        with pytest.raises(RuntimeError):
            await service.upload_file(env["app_id"], MD, "退货政策.md", created_by=owner)
        await s.rollback()

    async with env["sessions"]() as s:
        total = await s.scalar(
            select(func.count()).select_from(Document).where(Document.app_id == env["app_id"])
        )
    assert int(total or 0) == 0


# ---------------------------------------------------------------------- ③ 切块复核与启停
async def _seed_chunk(
    env: dict[str, Any],
    *,
    review_status: str = "flagged",
    enabled: bool = True,
    index_version: int = 1,
) -> dict[str, Any]:
    """直接落一份文档 + 一个切块（生效构建号），返回两个 id。"""
    async with env["sessions"]() as s:
        doc = Document(
            id=uuid.uuid4(),
            app_id=env["app_id"],
            title="退货政策.md",
            source_type="file",
            mime_type="text/markdown",
            size_bytes=len(MD),
            content_hash=uuid.uuid4().hex * 2,
            status="done",
            progress=1,
            chunk_count=1,
            index_version=1,
            tags=[],
        )
        s.add(doc)
        await s.flush()
        chunk = Chunk(
            id=uuid.uuid4(),
            app_id=env["app_id"],
            document_id=doc.id,
            index_version=index_version,
            ordinal=0,
            heading_path="第一章",
            text="自签收之日起 7 天内可退货。",
            search_text="自签收之日起 7 天内可退货。",
            tokens=12,
            enabled=enabled,
            review_status=review_status,
            flag_reason="疑似恶意指令" if review_status == "flagged" else None,
        )
        s.add(chunk)
        await s.commit()
    return {"document_id": doc.id, "chunk_id": chunk.id}


async def test_flagged_chunk_is_listed_and_can_be_approved(admin: dict[str, Any]) -> None:
    env, client = admin["env"], admin["client"]
    app_id = admin["app_id"]
    seeded = await _seed_chunk(env)

    listing = await client.get(
        f"/api/v1/admin/apps/{app_id}/chunks?review_status=flagged&document_id={seeded['document_id']}",
        headers=auth(),
    )

    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["document_title"] == "退货政策.md"
    assert item["flag_reason"] == "疑似恶意指令"

    patched = await client.patch(
        f"/api/v1/admin/apps/{app_id}/chunks/{seeded['chunk_id']}",
        headers=auth(),
        json={"review_status": "approved"},
    )

    assert patched.status_code == 200
    assert patched.json()["review_status"] == "approved"
    assert patched.json()["enabled"] is True
    assert patched.json()["reviewed_at"] is not None

    remaining = await client.get(
        f"/api/v1/admin/apps/{app_id}/chunks?review_status=flagged", headers=auth()
    )
    assert remaining.json()["total"] == 0

    audits = await _audit_rows(env, "kb.chunk.update")
    assert len(audits) == 1
    assert audits[0].target_type == "chunk" and audits[0].target_id == seeded["chunk_id"]


async def test_disabled_chunk_leaves_the_retrievable_set(admin: dict[str, Any]) -> None:
    env, client = admin["env"], admin["client"]
    app_id = admin["app_id"]
    seeded = await _seed_chunk(env, review_status="none")

    async def count() -> int:
        async with env["sessions"]() as s:
            service = KnowledgeService(s, storage=env["storage"], store=env["store"])
            return await service.retrievable_chunk_count(app_id)

    before = await count()
    patched = await client.patch(
        f"/api/v1/admin/apps/{app_id}/chunks/{seeded['chunk_id']}",
        headers=auth(),
        json={"enabled": False},
    )
    after = await count()

    assert patched.status_code == 200 and patched.json()["enabled"] is False
    assert after == before - 1


async def test_chunk_from_old_build_is_404(admin: dict[str, Any]) -> None:
    env, client = admin["env"], admin["client"]
    seeded = await _seed_chunk(env, index_version=0)

    r = await client.patch(
        f"/api/v1/admin/apps/{admin['app_id']}/chunks/{seeded['chunk_id']}",
        headers=auth(),
        json={"enabled": False},
    )

    assert r.status_code == 404
    assert r.json()["error"]["code"] == "CHUNK_NOT_FOUND"


async def test_unknown_chunk_is_404_and_writes_no_audit(
    admin: dict[str, Any],
) -> None:
    env, client = admin["env"], admin["client"]

    r = await client.patch(
        f"/api/v1/admin/apps/{admin['app_id']}/chunks/{uuid.uuid4()}",
        headers=auth(),
        json={"enabled": False},
    )

    assert r.status_code == 404
    assert await _audit_rows(env, "kb.chunk.update") == []
