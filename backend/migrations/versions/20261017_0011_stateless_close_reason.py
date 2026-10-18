"""M3.3：无状态会话的关闭原因（OpenAI 兼容，设计讨论 0009 D7）

- `conversations.close_reason` 的 CHECK 增加 `stateless`：不带 `X-AICS-Conversation-Id`
  的请求（OpenAI 兼容）本轮结束即关闭本轮建出的会话，不参与空闲提醒、不做记忆压缩。

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-17
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = ("idle", "handoff_idle", "app_deleted", "eval_done")
NEW = ("idle", "handoff_idle", "app_deleted", "eval_done", "stateless")


def _check(values: tuple[str, ...]) -> str:
    return f"close_reason IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_conversations_close_reason"), "conversations", type_="check")
    op.create_check_constraint(op.f("ck_conversations_close_reason"), "conversations", _check(NEW))


def downgrade() -> None:
    # `stateless` 会话按系统主动关闭归档成 idle（回滚不区分两者）
    op.execute("UPDATE conversations SET close_reason = 'idle' WHERE close_reason = 'stateless'")
    op.drop_constraint(op.f("ck_conversations_close_reason"), "conversations", type_="check")
    op.create_check_constraint(op.f("ck_conversations_close_reason"), "conversations", _check(OLD))
