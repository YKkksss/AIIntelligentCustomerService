import json

import pytest
from pydantic import ValidationError

from aics.core.config import CELERY_QUEUES, Settings, get_settings
from tests.conftest import MODELS


def make(**env: str) -> Settings:
    return Settings(_env_file=None, **env)  # type: ignore[arg-type]


def test_loads_from_env() -> None:
    s = get_settings()
    assert s.env == "test"
    assert s.llm_model == "deepseek-flash"
    assert s.models.get("deepseek-flash").context_window == 1_000_000  # type: ignore[union-attr]
    assert s.cors_origins == ["http://localhost:5173"]
    assert CELERY_QUEUES == ("ingest", "context", "eval", "maintenance")


def test_csv_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AICS_CORS_ORIGINS", "http://a, http://b")
    monkeypatch.setenv("AICS_URL_FETCH_ALLOWLIST", "192.168.110.0/24,wiki.corp.local")
    s = make()
    assert s.cors_origins == ["http://a", "http://b"]
    assert s.url_fetch_allowlist == ["192.168.110.0/24", "wiki.corp.local"]


def test_empty_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AICS_URL_FETCH_ALLOWLIST", "")
    assert make().url_fetch_allowlist == []


def test_trusted_proxies_accepts_cidrs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AICS_TRUSTED_PROXIES", "10.0.0.0/8, 2001:db8::/32")
    assert make().trusted_proxies == ["10.0.0.0/8", "2001:db8::/32"]


def test_trusted_proxies_rejects_non_cidr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AICS_TRUSTED_PROXIES", "10.0.0.0/8,nginx")
    with pytest.raises(ValidationError, match="CIDR"):
        make()


def test_secrets_not_in_repr() -> None:
    s = get_settings()
    assert "x" * 64 not in repr(s)
    assert "sk-test" not in repr(s)


@pytest.mark.parametrize(
    "var", ["AICS_JWT_SECRET", "AICS_DB_URL", "AICS_MODELS", "AICS_LLM_API_KEY"]
)
def test_missing_required(monkeypatch: pytest.MonkeyPatch, var: str) -> None:
    monkeypatch.delenv(var)
    with pytest.raises(ValidationError):
        make()


def test_jwt_secret_too_short(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AICS_JWT_SECRET", "short")
    with pytest.raises(ValidationError, match="64"):
        make()


def test_db_url_requires_asyncpg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AICS_DB_URL", "postgresql://u:p@h/db")
    with pytest.raises(ValidationError, match="asyncpg"):
        make()


@pytest.mark.parametrize(
    ("var", "value", "msg"),
    [
        ("AICS_LLM_MODEL", "gpt-unknown", "不在 AICS_MODELS"),
        ("AICS_EMBED_MODEL", "deepseek-flash", "kind 为 chat"),
        ("AICS_RERANK_MODEL", "nope", "AICS_RERANK_MODEL"),
    ],
)
def test_default_models_must_be_registered(
    monkeypatch: pytest.MonkeyPatch, var: str, value: str, msg: str
) -> None:
    monkeypatch.setenv(var, value)
    with pytest.raises(ValidationError, match=msg):
        make()


def test_rerank_none_skips_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AICS_RERANK_PROVIDER", "none")
    monkeypatch.setenv("AICS_RERANK_MODEL", "anything")
    monkeypatch.delenv("AICS_RERANK_API_KEY")
    assert make().rerank_provider == "none"


def test_dashscope_rerank_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AICS_RERANK_API_KEY")
    with pytest.raises(ValidationError, match="RERANK_API_KEY"):
        make()


@pytest.mark.parametrize(
    "models",
    [
        [],
        [MODELS[0], MODELS[0], MODELS[1], MODELS[2]],
        [{**MODELS[0], "kind": "vision"}, MODELS[1], MODELS[2]],
        [{**MODELS[0], "context_window": 0}, MODELS[1], MODELS[2]],
        [{**MODELS[0], "price_in": -1}, MODELS[1], MODELS[2]],
        [{**MODELS[0], "extra": 1}, MODELS[1], MODELS[2]],
    ],
    ids=["empty", "duplicate", "bad-kind", "zero-window", "negative-price", "extra-field"],
)
def test_registry_validation(monkeypatch: pytest.MonkeyPatch, models: list[dict]) -> None:
    monkeypatch.setenv("AICS_MODELS", json.dumps(models))
    with pytest.raises(ValidationError):
        make()


def test_registry_queries() -> None:
    reg = get_settings().models
    assert [m.name for m in reg.by_kind("chat")] == ["deepseek-flash"]
    assert reg.get("missing") is None
    assert {m["name"] for m in reg.public()} == {m["name"] for m in MODELS}
