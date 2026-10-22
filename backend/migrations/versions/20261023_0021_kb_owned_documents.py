"""M4.7 ④：知识库拥有文档——把 app_id 放开为可空（设计讨论 0013）

知识库成为文档的归属方（`documents.kb_id`），`app_id` 退化为「上传来源 / 历史兼容」：
公共知识库页上传的文档没有所属智能客服，因此四张表的外键列改为可空。
存量数据不变（0018 已回填 `kb_id`，所有旧行仍带 `app_id`）。

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels = None
depends_on = None

TABLES = ("documents", "chunks", "chunk_parents", "chunk_questions")


def upgrade() -> None:
    for table in TABLES:
        op.alter_column(table, "app_id", existing_type=sa.UUID(), nullable=True)


def downgrade() -> None:
    for table in TABLES:
        op.alter_column(table, "app_id", existing_type=sa.UUID(), nullable=False)