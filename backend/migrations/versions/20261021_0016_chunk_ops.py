"""M4.3b ② 切块级运营的数据层（设计讨论 0010）

三处变更：

- `chunks.source`：`parsed`（解析自动切）/ `manual`（人工新增）/ `edited`（人工改过正文）。
  重建只替换 `parsed`；`manual` 迁移到新构建号；`edited` 按正文哈希匹配，失配降级 `manual`。
- `documents.effective_at` / `expires_at`：有效期进 `retrievable()` 的时间窗（PG 侧过滤，
  Qdrant 不存时间，见 ISSUE-96）。
- `documents.simhash`：64 位 SimHash，入库做近重复提示（海明 ≤ 3 只提示不阻止）。

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SOURCES = ("parsed", "manual", "edited")


def _check(values: tuple[str, ...]) -> str:
    return f"source IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.add_column(
        "chunks",
        sa.Column("source", sa.Text(), server_default=sa.text("'parsed'"), nullable=False),
    )
    op.create_check_constraint(op.f("ck_chunks_source"), "chunks", _check(SOURCES))
    op.add_column("documents", sa.Column("effective_at", sa.DateTime(timezone=True)))
    op.add_column("documents", sa.Column("expires_at", sa.DateTime(timezone=True)))
    op.add_column("documents", sa.Column("simhash", sa.BigInteger()))
    op.create_index(
        "ix_documents_expires_at",
        "documents",
        ["expires_at"],
        postgresql_where=sa.text("expires_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_documents_expires_at", table_name="documents")
    op.drop_column("documents", "simhash")
    op.drop_column("documents", "expires_at")
    op.drop_column("documents", "effective_at")
    op.drop_constraint(op.f("ck_chunks_source"), "chunks", type_="check")
    op.drop_column("chunks", "source")
