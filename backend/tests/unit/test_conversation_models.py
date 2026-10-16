"""M2.1: DDL of conversations / messages / system_settings matches the design docs."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from aics.db.models import Conversation, Message, SystemSetting
from aics.rag.config import PLATFORM_DEFAULT_SESSION

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations" / "versions"


def _ddl(model: type) -> str:
    table = model.__table__  # type: ignore[attr-defined]
    parts = [str(CreateTable(table).compile(dialect=postgresql.dialect()))]
    parts += [str(CreateIndex(ix).compile(dialect=postgresql.dialect())) for ix in table.indexes]
    return "\n".join(parts)


def test_conversations_ddl() -> None:
    ddl = _ddl(Conversation)
    for frag in [
        "app_id UUID NOT NULL",
        "status TEXT DEFAULT 'active' NOT NULL",
        "last_activity_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL",
        "idle_notice_sent_at TIMESTAMP WITH TIME ZONE",
        "handoff_status TEXT DEFAULT 'none' NOT NULL",
        "summary JSONB",
        "summary_until_msg_id UUID",
        "summary_version INTEGER DEFAULT 0 NOT NULL",
        "event_seq BIGINT DEFAULT 0 NOT NULL",
        "channel IN ('api', 'openai', 'debug', 'eval')",
        "handoff_status IN ('none', 'pending', 'accepted', 'released')",
        "ON DELETE SET NULL",
        "WHERE status = 'active'",
        "WHERE needs_callback",
        "last_message_at DESC",
    ]:
        assert frag in ddl, frag


def test_messages_ddl() -> None:
    ddl = _ddl(Message)
    for frag in [
        "role IN ('user', 'assistant', 'human_agent', 'system')",
        "kind TEXT DEFAULT 'chat' NOT NULL",
        "role = 'system' OR kind = 'chat'",
        "token_count INTEGER DEFAULT 0 NOT NULL",
        "search_text TEXT DEFAULT '' NOT NULL",
        "GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, search_text)) STORED",
        "USING gin (tsv)",
        "prompt_version TEXT",
        "citations JSONB",
        "intent IN ('kb_qa', 'chitchat', 'human', 'out_of_scope', 'action', 'clarify', 'blocked')",
        "intent_confidence BETWEEN 0 AND 1",
        "ON DELETE CASCADE",
        "DEFAULT clock_timestamp()",
    ]:
        assert frag in ddl, frag


def test_system_settings_ddl() -> None:
    ddl = _ddl(SystemSetting)
    assert "key TEXT NOT NULL" in ddl and "value JSONB NOT NULL" in ddl
    assert "PRIMARY KEY (key)" in ddl


def test_seeded_default_session_equals_platform_default() -> None:
    path = next(MIGRATIONS.glob("*_0006_*.py"))
    spec = importlib.util.spec_from_file_location("m0006", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    expected = PLATFORM_DEFAULT_SESSION.model_dump()
    assert expected == mod.DEFAULT_SESSION_CONFIG
