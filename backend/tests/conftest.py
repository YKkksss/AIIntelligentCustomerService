"""测试公共夹具：假配置（全部为占位值，不含任何真实密钥）。"""

from __future__ import annotations

import json

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
