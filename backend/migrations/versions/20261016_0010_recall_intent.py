"""M2.6：recall 意图（回问已记信息，设计讨论 0007）

- `messages.intent` CHECK 增加 `recall`；
- 已存应用的 `rag_config.intent.enabled_intents` 若包含旧的全部 6 个意图（= 默认全开），
  追加 `recall`，使其与新建应用的默认值一致；自定义过（关掉某些意图）的应用不改。

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = ("kb_qa", "chitchat", "human", "out_of_scope", "action", "clarify", "blocked")
NEW = ("kb_qa", "chitchat", "human", "out_of_scope", "action", "clarify", "recall", "blocked")


def _check(values: tuple[str, ...]) -> str:
    return f"intent IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_messages_intent"), "messages", type_="check")
    op.create_check_constraint(op.f("ck_messages_intent"), "messages", _check(NEW))
    op.execute(
        """
        UPDATE apps
        SET rag_config = jsonb_set(
            rag_config, '{intent,enabled_intents}',
            (rag_config #> '{intent,enabled_intents}') || '["recall"]'::jsonb)
        WHERE jsonb_typeof(rag_config #> '{intent,enabled_intents}') = 'array'
          AND (rag_config #> '{intent,enabled_intents}')
              @> '["kb_qa","chitchat","human","out_of_scope","action","clarify"]'::jsonb
          AND NOT (rag_config #> '{intent,enabled_intents}') ? 'recall'
        """
    )


def downgrade() -> None:
    op.execute("UPDATE messages SET intent = 'kb_qa' WHERE intent = 'recall'")
    op.execute(
        """
        UPDATE apps
        SET rag_config = jsonb_set(
            rag_config, '{intent,enabled_intents}',
            (rag_config #> '{intent,enabled_intents}') - 'recall')
        WHERE (rag_config #> '{intent,enabled_intents}') ? 'recall'
        """
    )
    op.drop_constraint(op.f("ck_messages_intent"), "messages", type_="check")
    op.create_check_constraint(op.f("ck_messages_intent"), "messages", _check(OLD))
