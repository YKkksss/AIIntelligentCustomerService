"""apps.service 的配置校验 / 合并（不需要数据库）与新表 repository 覆盖。"""

from typing import Any, cast

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.schema import CreateTable

from aics.core.errors import ValidationAppError
from aics.db.base import Base
from aics.db.scoped import registered_repositories, unscoped_methods
from aics.modules.apps.service import AppService, _merge


def _svc() -> AppService:
    return AppService(cast(AsyncSession, object()))


def _build(**kw: Any) -> Any:
    args: dict[str, Any] = {
        "rag_config": None,
        "handoff_config": None,
        "session_config": None,
        "chunk_config": None,
    }
    args.update(kw)
    return _svc().build_config(**args)


def test_build_config_fills_defaults() -> None:
    cfg = _build(rag_config={"vector_top_k": 8})
    assert cfg.rag.vector_top_k == 8 and cfg.rag.intent.enabled
    assert cfg.effective_session.idle_close_minutes == 10
    assert cfg.chunk.chunk_size == 500 and cfg.webhook.url is None


def test_invalid_config_raises_validation_error_with_all_errors() -> None:
    with pytest.raises(ValidationAppError) as e:
        _build(
            rag_config={"intent": {"enabled_intents": ["chitchat"]}},
            chunk_config={"chunk_size": 10},
            session_config={"idle_close_minutes": 0},
        )
    err = e.value
    assert err.code == "VALIDATION_ERROR" and err.status == 422
    roots = {x["loc"][0] for x in err.details["errors"]}
    assert roots == {"rag_config", "chunk_config", "session_config"}


def test_unknown_model_and_window_are_validation_errors() -> None:
    with pytest.raises(ValidationAppError) as e:
        _build(rag_config={"llm_model": "gpt-x"})
    assert e.value.details["errors"][0]["loc"] == ["rag_config", "llm_model"]
    # conftest 的 deepseek-flash 窗口 1M：大 chunk 也通过
    _build(rag_config={"rerank_top_n": 20}, chunk_config={"chunk_size": 2000, "chunk_overlap": 0})


def test_queue_timeout_vs_effective_idle_close() -> None:
    with pytest.raises(ValidationAppError):
        _build(session_config={"idle_close_minutes": 3, "idle_notice_before_minutes": 1})


def test_merge_nested_intent_only() -> None:
    cur = {"vector_top_k": 20, "intent": {"enabled": True, "min_confidence": 0.6}}
    out = _merge(cur, {"intent": {"min_confidence": 0.5}, "vector_top_k": 5}, ("intent",))
    assert out == {"vector_top_k": 5, "intent": {"enabled": True, "min_confidence": 0.5}}
    sh = {"service_hours": {"timezone": "UTC", "weekly": {"mon": []}}}
    assert _merge(sh, {"service_hours": None}, ()) == {"service_hours": None}


def test_basic_field_checks() -> None:
    with pytest.raises(ValidationAppError):
        AppService._check_basic({"name": "  "})
    with pytest.raises(ValidationAppError):
        AppService._check_basic({"answer_style": "funny"})


def test_every_app_scoped_table_has_scoped_repository() -> None:
    """所有带 app_id 的表都有继承 AppScopedRepository 的 repository，且签名合规。"""
    import aics.db.models  # noqa: F401
    import aics.memory.repository  # noqa: F401
    import aics.modules.apikeys.repository  # noqa: F401
    import aics.modules.apps.repository  # noqa: F401
    import aics.modules.chat.event_repository  # noqa: F401
    import aics.modules.chat.repository  # noqa: F401
    import aics.modules.feedback.repository  # noqa: F401
    import aics.modules.knowledge.repository  # noqa: F401
    import aics.modules.webhooks.repository  # noqa: F401

    repos = [r for r in registered_repositories() if r.__module__.startswith("aics.")]
    covered = {r.model.__tablename__ for r in repos}  # type: ignore[attr-defined]
    tenant_tables = {t.name for t in Base.metadata.sorted_tables if "app_id" in t.c} | {"apps"}
    assert tenant_tables == {
        "apps",
        "user_app_roles",
        "documents",
        "chunks",
        "chunk_parents",
        "chunk_questions",
        "jobs",
        "conversations",
        "messages",
        "conversation_facts",
        "conversation_fact_history",
        "conversation_events",
        "webhook_deliveries",
        "feedback",
        "api_keys",
    }
    assert tenant_tables <= covered
    for r in repos:
        assert unscoped_methods(r) == [], r


def test_ddl_key_columns() -> None:
    import aics.db.models as m

    chunks = str(CreateTable(m.Chunk.__table__).compile(dialect=postgresql.dialect()))
    assert "GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, search_text)) STORED" in chunks
    docs = str(CreateTable(m.Document.__table__).compile(dialect=postgresql.dialect()))
    assert "content_hash CHAR(64) NOT NULL" in docs and "metadata JSONB" in docs
    apps = str(CreateTable(m.App.__table__).compile(dialect=postgresql.dialect()))
    for col in [
        "rag_config JSONB NOT NULL",
        "session_config JSONB DEFAULT '{}' NOT NULL",
        "custom_dict TEXT[] DEFAULT '{}' NOT NULL",
        "app_id",
    ]:
        if col == "app_id":
            assert " app_id " not in apps  # synonym，不是真实列
        else:
            assert col in apps, col
