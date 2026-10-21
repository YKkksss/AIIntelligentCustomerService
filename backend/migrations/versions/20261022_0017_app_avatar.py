"""智能客服头像（`apps.avatar`）。

只加一列 JSONB，默认 `{}` 表示「用默认头像」（按应用 id 派生稳定的 seed）：

    {"style": "notionists", "seed": "6f0c…", "url": null}

不存图片、不建索引：前端用 DiceBear 按 style + seed 在浏览器里画，服务端零文件。
历史行靠默认值自动补齐，无需回填。

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "apps",
        sa.Column("avatar", JSONB(), server_default=sa.text("'{}'"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("apps", "avatar")
