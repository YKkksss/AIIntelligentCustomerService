"""测试公共夹具：假配置（全部为占位值，不含任何真实密钥）。"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest

MODELS = [
    {
        "name": "deepseek-flash",
        "kind": "chat",
        "context_window": 1_000_000,
        "price_in": 2.0,
        "price_out": 8.0,
    },
    {
        "name": "qwen3.7-text-embedding-flash",
        "kind": "embedding",
        "context_window": 8192,
        "price_in": 0.125,
        "price_out": 0,
    },
    {
        "name": "qwen3.7-text-rerank",
        "kind": "rerank",
        "context_window": 8192,
        "price_in": 0.5,
        "price_out": 0,
    },
]

TEST_ENV = {
    "AICS_ENV": "test",
    "AICS_LOG_JSON": "true",
    "AICS_JWT_SECRET": "x" * 64,
    "AICS_API_KEY_PEPPER": "p" * 32,
    "AICS_DATA_ENCRYPTION_KEY": "test-fernet-key-placeholder",
    "AICS_DB_URL": "postgresql+asyncpg://aics:pwd@127.0.0.1:5432/aics_test",
    "AICS_REDIS_URL": "redis://127.0.0.1:6379/7",
    "AICS_CELERY_BROKER_URL": "amqp://aics:pwd@127.0.0.1:5672/aics",
    "AICS_QDRANT_URL": "http://127.0.0.1:6333",
    "AICS_QDRANT_API_KEY": "qdrant-test",
    "AICS_LLM_API_KEY": "sk-test",
    "AICS_EMBED_API_KEY": "sk-test",
    "AICS_RERANK_API_KEY": "sk-test",
    "AICS_MODELS": json.dumps(MODELS),
}


@pytest.fixture(autouse=True)
def test_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    from aics.core.config import get_settings

    for k, v in TEST_ENV.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    yield TEST_ENV
    get_settings.cache_clear()


# --- 真 PG 的公共夹具（M3.6 ④ 从 tests/integration/conftest.py 上移） -----------------
# 放进根 conftest 是因为 `tests/e2e`（接入指南示例）与 `tests/integration` 都要用；
# `DB` 在函数内 import，避免单元测试收集时也去导入 qdrant / storage 依赖。


@pytest.fixture
def pg_migrated(monkeypatch: pytest.MonkeyPatch) -> str:
    """PG only (no Qdrant): 迁移到 head。"""

    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    from aics.core.config import get_settings

    # 与 `tests/integration/ingest_support.py` 的 `DB` 同一个环境变量；不 import 它，
    # 因为那个模块只在收集 `tests/integration` 时在 sys.path 上。
    db = os.environ.get("AICS_TEST_DB_URL", "")
    if not db:
        pytest.skip("未设置 AICS_TEST_DB_URL")
    monkeypatch.setenv("AICS_DB_URL", db)
    get_settings.cache_clear()
    command.upgrade(Config(str(Path(__file__).resolve().parents[1] / "alembic.ini")), "head")
    return db


@pytest.fixture
async def pg(pg_migrated: str):
    """两个新应用（A、B）与一个用户；结束时按外键顺序清理。"""

    import uuid

    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from aics.db.models import ApiKey, App, Chunk, ChunkParent, Conversation, Document, Job, User
    from aics.modules.apps.service import AppService

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
            # messages / facts / fact history / events / deliveries cascade from conversations
            await s.execute(delete(Conversation).where(Conversation.app_id.in_(ids)))
            await s.execute(delete(ApiKey).where(ApiKey.app_id.in_(ids)))
            # 用例可能直接插过文档（如隔离用例的「别人的资源」），不清会挡住 App 的删除
            for model in (Chunk, ChunkParent, Job, Document):
                await s.execute(delete(model).where(model.app_id.in_(ids)))
            await s.execute(delete(App).where(App.id.in_(ids)))
            await s.execute(delete(User).where(User.id == user_id))
            await s.commit()
        await engine.dispose()


# --- 真 Redis 的公共夹具（M3.6 ④ 从 tests/integration/conftest.py 上移） -------------------
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
