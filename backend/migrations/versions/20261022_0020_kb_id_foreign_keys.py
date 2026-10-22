"""给 documents / chunks / chunk_questions 的 kb_id 补外键（M4.7 ③）。

0018 只加了列与索引，模型里 `kb_id` 声明了 `ForeignKey("knowledge_bases.id")`；不补外键会让
`alembic check`（迁移往返用例）一直报「Detected added foreign key」。这里把三条外键补齐，
语义为 `ON DELETE RESTRICT`：删库前必须先处理库里的文档（服务层已用 409 拦住）。

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-22
"""

from __future__ import annotations

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

TABLES = ("documents", "chunks", "chunk_questions")


def upgrade() -> None:
    for table in TABLES:
        op.create_foreign_key(
            op.f(f"fk_{table}_kb_id_knowledge_bases"),
            table,
            "knowledge_bases",
            ["kb_id"],
            ["id"],
        )


def downgrade() -> None:
    for table in TABLES:
        op.drop_constraint(op.f(f"fk_{table}_kb_id_knowledge_bases"), table, type_="foreignkey")
