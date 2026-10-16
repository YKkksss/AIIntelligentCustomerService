"""M1.4：documents.indexed_title（上下文增强向量化使用的标题）

数据库设计 §3.6；文档处理流水线 §3 第 6 步。

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("indexed_title", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "indexed_title")
