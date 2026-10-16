"""集成测试 fixture：迁移到 head 的 PG + aics_test_ 前缀 Qdrant 集合 + 每个测试一个新应用。"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from ingest_support import DB, DIM, QKEY, QURL
from qdrant_client import AsyncQdrantClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aics.core.config import get_settings
from aics.db.models import ApiKey, App, Chunk, ChunkParent, Conversation, Document, Job, User
from aics.modules.apps.service import AppService
from aics.storage.local import LocalStorage
from aics.vectorstore import QdrantVectorStore


@pytest.fixture
def ingest_migrated(monkeypatch: pytest.MonkeyPatch) -> str:
    if not (DB and QURL):
        pytest.skip("未设置 AICS_TEST_DB_URL / AICS_TEST_QDRANT_URL")
    monkeypatch.setenv("AICS_DB_URL", DB)
    monkeypatch.setenv("AICS_EMBED_BATCH", "2")
    get_settings.cache_clear()
    command.upgrade(Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")), "head")
    return DB


@pytest.fixture
def pg_migrated(monkeypatch: pytest.MonkeyPatch) -> str:
    """PG only (no Qdrant): migrated to head."""
    if not DB:
        pytest.skip("未设置 AICS_TEST_DB_URL")
    monkeypatch.setenv("AICS_DB_URL", DB)
    get_settings.cache_clear()
    command.upgrade(Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")), "head")
    return DB


@pytest.fixture
async def pg(pg_migrated: str) -> AsyncIterator[dict[str, Any]]:
    """Two fresh apps (A, B) for isolation tests; their conversations are removed afterwards."""
    engine = create_async_engine(pg_migrated)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as s:
        u = User(email=f"u{uuid.uuid4().hex[:8]}@x.io", display_name="u", password_hash="h")
        s.add(u)
        await s.flush()
        svc = AppService(s)
        a = await svc.create_app(name=f"it-{uuid.uuid4().hex[:6]}", created_by=u.id)
        b = await svc.create_app(name=f"it-{uuid.uuid4().hex[:6]}", created_by=u.id)
        await s.commit()
        ids, user_id = [a.id, b.id], u.id
    try:
        yield {"sessions": sessions, "app_a": ids[0], "app_b": ids[1], "user_id": user_id}
    finally:
        async with sessions() as s:
            # messages / facts / fact history cascade from conversations
            await s.execute(delete(Conversation).where(Conversation.app_id.in_(ids)))
            await s.execute(delete(ApiKey).where(ApiKey.app_id.in_(ids)))
            await s.execute(delete(App).where(App.id.in_(ids)))
            await s.execute(delete(User).where(User.id == user_id))
            await s.commit()
        await engine.dispose()


@pytest.fixture
async def env(ingest_migrated: str, tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    engine = create_async_engine(ingest_migrated)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    # AICS_TEST_QDRANT_URL=":memory:" → qdrant-client local mode (dev boxes without a server)
    client = (
        AsyncQdrantClient(location=":memory:")
        if QURL == ":memory:"
        else AsyncQdrantClient(url=QURL, api_key=QKEY, check_compatibility=False, timeout=10)
    )
    store = QdrantVectorStore(
        client, prefix="aics_test_", embed_model=f"it-{uuid.uuid4().hex[:8]}", dim=DIM
    )
    storage = LocalStorage(tmp_path)
    async with sessions() as s:
        u = User(email=f"u{uuid.uuid4().hex[:8]}@x.io", display_name="u", password_hash="h")
        s.add(u)
        await s.flush()
        app = await AppService(s).create_app(name=f"it-{uuid.uuid4().hex[:6]}", created_by=u.id)
        await s.commit()
        app_id, user_id = app.id, u.id
    ctx: dict[str, Any] = {
        "sessions": sessions,
        "store": store,
        "storage": storage,
        "app_id": app_id,
    }
    try:
        yield ctx
    finally:
        async with sessions() as s:
            for m in (Chunk, ChunkParent, Job, Document):
                await s.execute(delete(m).where(m.app_id == app_id))
            await s.execute(delete(App).where(App.id == app_id))
            await s.execute(delete(User).where(User.id == user_id))
            await s.commit()
        await store.drop_chunks_collection()
        await client.close()
        await engine.dispose()


REDIS_URL = os.environ.get("AICS_TEST_REDIS_URL", "redis://127.0.0.1:6379/7")


@pytest.fixture
async def redis_it() -> AsyncIterator[Any]:
    """Real Redis (test db 7); skipped when unreachable."""
    import redis.asyncio as aioredis

    client = aioredis.from_url(REDIS_URL)
    try:
        await client.ping()
    except Exception:
        pytest.skip("redis not reachable")
    yield client
    await client.aclose()
