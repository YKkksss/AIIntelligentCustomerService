"""M3.2 ③ 知识库写接口的 HTTP 集成用例（真 PG + 本地存储 + 假嵌入器/假抓取器，不跑 worker）。

只覆盖单测看不到的部分：multipart 流式读取与扩展名/大小把关、按内容去重、`Idempotency-Key`
重放与冲突、入队参数、URL 导入。真正的解析/切块/入库由 `test_knowledge_service.py`、
`test_ingest_pipeline.py` 与 `scripts/e2e_m1` 覆盖；这里到“建好作业并投递”为止。

服务器复验：本机没有 PG/Redis，这些用例与 `test_isolation_pg.py` 一起列入待复验清单。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from ingest_support import FakeEmbedder
from sqlalchemy import func, select, update

from aics.core.config import get_settings
from aics.core.idempotency import HEADER as IDEMPOTENCY_HEADER
from aics.core.idempotency import Idempotency
from aics.db.models import Document
from aics.ingest.parsers.url import FetchedUrl
from aics.main import create_app
from aics.modules.apikeys.service import ApiKeyAuthenticator, ApiKeyService
from aics.modules.knowledge import router as knowledge_router
from aics.modules.knowledge.runtime import KnowledgeRuntime
from aics.modules.knowledge.service import INGEST_TASK

pytestmark = pytest.mark.integration

MD = "# 退货政策\n\n7 天内可退回，运费由我们承担。\n".encode()


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _file(filename: str = "退货政策.md", data: bytes = MD) -> dict[str, Any]:
    return {"file": (filename, data, "text/markdown")}


def build_app(
    env: dict[str, Any],
    redis: Any,
    *,
    sent: list[tuple[str, list[Any]]] | None = None,
    fetched: list[str] | None = None,
) -> FastAPI:
    """真 PG/存储/Qdrant 客户端 + 假嵌入器；投递不经过 Celery，直接记进 `sent`。"""
    app = create_app()
    app.state.authenticator = ApiKeyAuthenticator(
        env["sessions"],
        pepper=get_settings().api_key_pepper.get_secret_value(),
        redis=redis,
    )
    app.state.idempotency = Idempotency(redis, prefix=get_settings().redis_prefix)
    app.state.knowledge_runtime = KnowledgeRuntime(
        sessions=env["sessions"],
        storage=env["storage"],
        vector=env["store"],
        embedder=FakeEmbedder(),
        enqueue=(lambda task, args: sent.append((task, list(args)))) if sent is not None else None,
        fetcher=_fetcher(fetched) if fetched is not None else None,
    )
    return app


def _fetcher(seen: list[str]) -> Any:
    async def fetch(url: str) -> FetchedUrl:
        seen.append(url)
        return FetchedUrl(url=url, content=MD, content_type="text/markdown", file_type="md")

    return fetch


async def new_key(env: dict[str, Any], redis: Any) -> str:
    pepper = get_settings().api_key_pepper.get_secret_value()
    async with env["sessions"]() as s:
        _, plain = await ApiKeyService(s, pepper=pepper, redis=redis).create(
            env["app_id"], name="knowledge-it", scopes=["kb:read", "kb:write"], rpm_limit=1000
        )
        await s.commit()
    return plain


async def count_documents(env: dict[str, Any]) -> int:
    async with env["sessions"]() as s:
        total = await s.scalar(
            select(func.count()).select_from(Document).where(Document.app_id == env["app_id"])
        )
    return int(total or 0)


async def mark_done(env: dict[str, Any], document_id: str) -> None:
    """本套用例不跑 worker：上传只建出 `queued` 文档，替身把首建跑完再测 replace / reindex。

    否则 `DOCUMENT_BUSY` 分支会把它们挡在 409（这是真实生产语义，不是测试缺陷）。
    """
    async with env["sessions"]() as s:
        await s.execute(
            update(Document)
            .where(Document.app_id == env["app_id"], Document.id == uuid.UUID(document_id))
            .values(status="done", progress=100, index_version=1, chunk_count=1)
        )
        await s.commit()


@pytest.fixture
async def api(env: dict[str, Any], redis_it: Any) -> AsyncIterator[dict[str, Any]]:
    sent: list[tuple[str, list[Any]]] = []
    fetched: list[str] = []
    app = build_app(env, redis_it, sent=sent, fetched=fetched)
    key = await new_key(env, redis_it)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t", timeout=30) as client:
        yield {
            "client": client,
            "key": key,
            "app_id": env["app_id"],
            "env": env,
            "sent": sent,
            "fetched": fetched,
        }


async def test_upload_creates_document_and_enqueues_ingest(api: dict[str, Any]) -> None:
    r = await api["client"].post(
        f"/api/v1/apps/{api['app_id']}/documents",
        files=_file(),
        data={"title": "退货政策"},
        headers=auth(api["key"]),
    )

    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "queued" and body["progress"] == 0
    assert body["duplicate"] is False and body["unchanged"] is False
    assert body["title"] == "退货政策" and body["job_id"]

    task, args = api["sent"][0]
    assert task == INGEST_TASK
    assert args == [body["id"], body["job_id"]]

    r = await api["client"].get(
        f"/api/v1/apps/{api['app_id']}/documents/{body['id']}", headers=auth(api["key"])
    )
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "退货政策"
    assert r.json()["status"] == "queued"


async def test_upload_same_content_is_duplicate(api: dict[str, Any]) -> None:
    """不传 `Idempotency-Key` 时走内容去重：同一份内容第二次返回 200 + `duplicate`，不再建作业。"""
    url = f"/api/v1/apps/{api['app_id']}/documents"
    first = await api["client"].post(url, files=_file(), headers=auth(api["key"]))
    second = await api["client"].post(url, files=_file(), headers=auth(api["key"]))

    assert first.status_code == 202, first.text
    assert second.status_code == 200, second.text
    assert second.json()["duplicate"] is True
    assert second.json()["id"] == first.json()["id"]
    assert await count_documents(api["env"]) == 1
    assert len(api["sent"]) == 1


async def test_upload_rejects_unknown_extension(api: dict[str, Any]) -> None:
    r = await api["client"].post(
        f"/api/v1/apps/{api['app_id']}/documents",
        files=_file(filename="木马.exe"),
        headers=auth(api["key"]),
    )

    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "UNSUPPORTED_FILE_TYPE"
    assert await count_documents(api["env"]) == 0


async def test_upload_rejects_oversized_file(
    api: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """上限按 `AICS_UPLOAD_MAX_MB` 判定，超出立即 413（不整体读入内存）。"""
    settings = get_settings().model_copy(update={"upload_max_mb": 0})
    monkeypatch.setattr(knowledge_router, "get_settings", lambda: settings)

    r = await api["client"].post(
        f"/api/v1/apps/{api['app_id']}/documents", files=_file(), headers=auth(api["key"])
    )

    assert r.status_code == 413, r.text
    assert r.json()["error"]["code"] == "FILE_TOO_LARGE"
    assert await count_documents(api["env"]) == 0


async def test_upload_replays_with_the_same_idempotency_key(api: dict[str, Any]) -> None:
    url = f"/api/v1/apps/{api['app_id']}/documents"
    headers = {**auth(api["key"]), IDEMPOTENCY_HEADER: "it-key-1"}

    first = await api["client"].post(url, files=_file(), headers=headers)
    second = await api["client"].post(url, files=_file(), headers=headers)

    assert first.status_code == 202, first.text
    assert second.status_code == 202, second.text
    assert second.json() == first.json()
    assert await count_documents(api["env"]) == 1
    assert len(api["sent"]) == 1


async def test_same_key_with_different_file_conflicts(api: dict[str, Any]) -> None:
    url = f"/api/v1/apps/{api['app_id']}/documents"
    headers = {**auth(api["key"]), IDEMPOTENCY_HEADER: "it-key-2"}
    await api["client"].post(url, files=_file(), headers=headers)

    r = await api["client"].post(
        url, files=_file(data=MD + "新增一行。\n".encode()), headers=headers
    )

    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


async def test_url_import_uses_the_fetcher(api: dict[str, Any]) -> None:
    r = await api["client"].post(
        f"/api/v1/apps/{api['app_id']}/documents/url",
        json={"url": "https://example.com/return-policy", "title": "官网退货政策"},
        headers=auth(api["key"]),
    )

    assert r.status_code == 202, r.text
    assert api["fetched"] == ["https://example.com/return-policy"]
    assert api["sent"][0][0] == INGEST_TASK
    document_id = r.json()["id"]

    r = await api["client"].get(
        f"/api/v1/apps/{api['app_id']}/documents/{document_id}", headers=auth(api["key"])
    )
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "官网退货政策"


async def test_replace_with_identical_content_is_unchanged(api: dict[str, Any]) -> None:
    url = f"/api/v1/apps/{api['app_id']}/documents"
    created = await api["client"].post(url, files=_file(), headers=auth(api["key"]))
    document_id = created.json()["id"]
    await mark_done(api["env"], document_id)

    r = await api["client"].put(
        f"{url}/{document_id}/file", files=_file(), headers=auth(api["key"])
    )

    assert r.status_code == 200, r.text
    assert r.json()["unchanged"] is True
    assert r.json()["id"] == document_id
    assert len(api["sent"]) == 1  # 内容没变 → 不投递新作业


async def test_reindex_returns_202_and_enqueues_ingest(api: dict[str, Any]) -> None:
    """文档级重建走 `aics.ingest_document`（应用级重新分词 `aics.retokenize_app` 是另一个作业）。"""
    url = f"/api/v1/apps/{api['app_id']}/documents"
    created = await api["client"].post(url, files=_file(), headers=auth(api["key"]))
    document_id = created.json()["id"]
    await mark_done(api["env"], document_id)

    r = await api["client"].post(f"{url}/{document_id}/reindex", headers=auth(api["key"]))

    assert r.status_code == 202, r.text
    assert r.json()["id"] == document_id
    assert r.json()["job_id"]
    assert api["sent"][-1][0] == INGEST_TASK


async def test_unknown_app_id_under_another_keys_path_is_404(api: dict[str, Any]) -> None:
    """路径里的 app_id 不是这个 Key 的应用时一律 404，不泄露存在性。"""
    other = uuid.uuid4()

    r = await api["client"].post(
        f"/api/v1/apps/{other}/documents", files=_file(), headers=auth(api["key"])
    )

    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "APP_NOT_FOUND"
