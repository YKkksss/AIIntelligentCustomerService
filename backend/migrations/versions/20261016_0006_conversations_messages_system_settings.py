"""M2.1: conversations, messages and system_settings

数据库设计 §3.8, §3.9, §3.14; 对话上下文管理 §5.1–5.2 (token_count / search_text / tsv and
summary* columns are part of this initial migration, see §5.4). Seeds the platform default
session config `system_settings.default_session_config`.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-16 16:24:57.526411+08:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Must equal aics.rag.config.PLATFORM_DEFAULT_SESSION (unit test guards this).
DEFAULT_SESSION_CONFIG = {
    "idle_close_minutes": 10,
    "idle_notice_before_minutes": 1,
    "idle_close_notice": "如果没有其他问题，当前服务就结束咯~",
}

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "system_settings",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_by", sa.UUID(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"], ["users.id"], name=op.f("fk_system_settings_updated_by_users")
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_system_settings")),
    )
    op.create_table(
        "conversations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("app_id", sa.UUID(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_reason", sa.Text(), nullable=True),
        sa.Column("previous_conversation_id", sa.UUID(), nullable=True),
        sa.Column(
            "last_activity_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("idle_notice_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("external_user_id", sa.Text(), nullable=True),
        sa.Column("api_key_id", sa.UUID(), nullable=True),
        sa.Column("created_by", sa.UUID(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("message_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("refusal_streak", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("handoff_status", sa.Text(), server_default=sa.text("'none'"), nullable=False),
        sa.Column("handoff_reason", sa.Text(), nullable=True),
        sa.Column("handoff_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("handoff_accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("handoff_released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("handoff_end_reason", sa.Text(), nullable=True),
        sa.Column("needs_callback", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("callback_reason", sa.Text(), nullable=True),
        sa.Column("callback_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("callback_cleared_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("callback_cleared_by", sa.UUID(), nullable=True),
        sa.Column("callback_note", sa.Text(), nullable=True),
        sa.Column("handoff_agent_id", sa.Text(), nullable=True),
        sa.Column("handoff_agent_name", sa.Text(), nullable=True),
        sa.Column("event_seq", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "summary", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True
        ),
        sa.Column("summary_until_msg_id", sa.UUID(), nullable=True),
        sa.Column("summary_version", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("summary_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "last_message_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'closed') = (closed_at IS NOT NULL)", name=op.f("ck_conversations_closed_at")
        ),
        sa.CheckConstraint(
            "callback_reason IN ('queue_timeout', 'offline')",
            name=op.f("ck_conversations_callback_reason"),
        ),
        sa.CheckConstraint(
            "channel IN ('api', 'openai', 'debug', 'eval')", name=op.f("ck_conversations_channel")
        ),
        sa.CheckConstraint(
            "close_reason IN ('idle', 'handoff_idle', 'app_deleted', 'eval_done')",
            name=op.f("ck_conversations_close_reason"),
        ),
        sa.CheckConstraint(
            "handoff_end_reason IN ('agent', 'admin', 'queue_timeout', 'offline', 'accepted_idle')",
            name=op.f("ck_conversations_handoff_end_reason"),
        ),
        sa.CheckConstraint(
            "handoff_status IN ('none', 'pending', 'accepted', 'released')",
            name=op.f("ck_conversations_handoff_status"),
        ),
        sa.CheckConstraint("status IN ('active', 'closed')", name=op.f("ck_conversations_status")),
        sa.CheckConstraint("message_count >= 0", name=op.f("ck_conversations_message_count")),
        sa.CheckConstraint("refusal_streak >= 0", name=op.f("ck_conversations_refusal_streak")),
        sa.CheckConstraint("summary_version >= 0", name=op.f("ck_conversations_summary_version")),
        sa.ForeignKeyConstraint(["app_id"], ["apps.id"], name=op.f("fk_conversations_app_id_apps")),
        sa.ForeignKeyConstraint(
            ["callback_cleared_by"],
            ["users.id"],
            name=op.f("fk_conversations_callback_cleared_by_users"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_conversations_created_by_users")
        ),
        sa.ForeignKeyConstraint(
            ["previous_conversation_id"],
            ["conversations.id"],
            name=op.f("fk_conversations_previous_conversation_id_conversations"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
    )
    op.create_index(
        "ix_conversations_active_activity",
        "conversations",
        ["last_activity_at"],
        unique=False,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_conversations_app_callback",
        "conversations",
        ["app_id", sa.literal_column("callback_requested_at DESC")],
        unique=False,
        postgresql_where=sa.text("needs_callback"),
    )
    op.create_index(
        "ix_conversations_app_external_user",
        "conversations",
        ["app_id", "external_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_conversations_app_handoff",
        "conversations",
        ["app_id", "handoff_status"],
        unique=False,
        postgresql_where=sa.text("handoff_status IN ('pending', 'accepted')"),
    )
    op.create_index(
        "ix_conversations_app_last_message",
        "conversations",
        ["app_id", sa.literal_column("last_message_at DESC")],
        unique=False,
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("conversation_id", sa.UUID(), nullable=False),
        sa.Column("app_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), server_default=sa.text("'chat'"), nullable=False),
        sa.Column("agent_id", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("search_text", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('simple'::regconfig, search_text)", persisted=True),
            nullable=False,
        ),
        sa.Column("answer_type", sa.Text(), nullable=True),
        sa.Column("intent", sa.Text(), nullable=True),
        sa.Column("intent_confidence", postgresql.REAL(), nullable=True),
        sa.Column("intent_source", sa.Text(), nullable=True),
        sa.Column(
            "slots", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True
        ),
        sa.Column("rewritten_query", sa.Text(), nullable=True),
        sa.Column(
            "citations", postgresql.JSONB(none_as_null=True, astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "retrieval_trace",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("handoff", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "degraded", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("first_token_ms", sa.Integer(), nullable=True),
        sa.Column("llm_model", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.Text(), nullable=True),
        sa.Column("config_version", sa.Integer(), nullable=True),
        sa.Column("interrupted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("clock_timestamp()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "answer_type IN ('answer', 'refused', 'handoff', 'error')",
            name=op.f("ck_messages_answer_type"),
        ),
        sa.CheckConstraint(
            "intent IN ('kb_qa', 'chitchat', 'human', 'out_of_scope', 'action', 'clarify', 'blocked')",
            name=op.f("ck_messages_intent"),
        ),
        sa.CheckConstraint(
            "intent_source IN ('rule', 'vector', 'llm', 'fallback')",
            name=op.f("ck_messages_intent_source"),
        ),
        sa.CheckConstraint(
            "kind IN ('chat', 'idle_notice', 'idle_closed', 'handoff_wait', 'handoff_accepted', 'handoff_released', 'handoff_queue_timeout', 'handoff_offline', 'handoff_idle_closed')",
            name=op.f("ck_messages_kind"),
        ),
        sa.CheckConstraint(
            "role = 'assistant' OR (answer_type IS NULL AND intent IS NULL AND intent_confidence IS NULL AND intent_source IS NULL AND citations IS NULL AND NOT handoff)",
            name=op.f("ck_messages_assistant_only_fields"),
        ),
        sa.CheckConstraint("role = 'system' OR kind = 'chat'", name=op.f("ck_messages_kind_role")),
        sa.CheckConstraint(
            "role IN ('user', 'assistant', 'human_agent', 'system')", name=op.f("ck_messages_role")
        ),
        sa.CheckConstraint(
            "intent_confidence BETWEEN 0 AND 1", name=op.f("ck_messages_intent_confidence")
        ),
        sa.CheckConstraint("token_count >= 0", name=op.f("ck_messages_token_count")),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_messages_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
    )
    op.create_index(
        "ix_messages_app_answer_type", "messages", ["app_id", "answer_type"], unique=False
    )
    op.create_index(
        "ix_messages_app_created",
        "messages",
        ["app_id", sa.literal_column("created_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_messages_app_intent_created",
        "messages",
        ["app_id", "intent", sa.literal_column("created_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_messages_conversation_created",
        "messages",
        ["conversation_id", "created_at"],
        unique=False,
    )
    op.create_index("ix_messages_tsv", "messages", ["tsv"], unique=False, postgresql_using="gin")
    settings = sa.table(
        "system_settings",
        sa.column("key", sa.Text()),
        sa.column("value", postgresql.JSONB()),
    )
    op.bulk_insert(settings, [{"key": "default_session_config", "value": DEFAULT_SESSION_CONFIG}])


def downgrade() -> None:
    op.drop_table("messages")
    op.drop_table("conversations")
    op.drop_table("system_settings")
