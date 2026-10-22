"""app_kb_links 增加「默认库」标记（M4.7 ③）。

一个客服可以关联多个公共知识库，但旧接口（`/api/v1/apps/{app_id}/documents`、
`/api/v1/admin/apps/{app_id}/documents`）只有一个作用域，需要一个明确的默认库：
每个应用最多一个 `is_default = true` 的关联；没有显式设置时取最早建立的那条。

Revision ID: 0019
Revises: 0018
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "app_kb_links",
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # 回填：每个应用最早的一条关联置为默认
    op.execute(
        sa.text(
            """
            UPDATE app_kb_links AS l
            SET is_default = true
            WHERE l.kb_id = (
                SELECT l2.kb_id FROM app_kb_links AS l2
                WHERE l2.app_id = l.app_id
                ORDER BY l2.created_at, l2.kb_id
                LIMIT 1
            )
            """
        )
    )
    op.create_index(
        "uq_app_kb_links_default",
        "app_kb_links",
        ["app_id"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )


def downgrade() -> None:
    op.drop_index("uq_app_kb_links_default", table_name="app_kb_links")
    op.drop_column("app_kb_links", "is_default")
