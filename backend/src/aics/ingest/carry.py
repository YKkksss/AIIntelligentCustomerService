"""重建时保留人工切块（M4.3b，设计讨论 0010 §2.1）。

重建的输入是原文件，人工改动无法从原文件推出来，所以：

- `manual`：整块搬到新构建号（新 id、ordinal 追加在新切块之后），需要重新向量化；
- `edited`：按正文哈希在新构建的切块里找同一段文本——命中就把新切块标成 `edited`（保留
  「人工改过」的来历），未命中走 `manual` 的路径。

纯函数 + 一次查询，不碰向量库：调用方（`_switch`）拿到 `EmbedItem` 列表后在会话外做
`embed_and_upsert`，避免长事务里等嵌入。
"""

from __future__ import annotations

import hashlib
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aics.db.base import new_id
from aics.db.models import Chunk
from aics.ingest.indexer import EmbedItem, build_embed_text


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def carry_chunks(
    session: AsyncSession,
    *,
    app_id: UUID,
    document_id: UUID,
    old_version: int,
    new_version: int,
    title: str,
    contextual_prefix: bool = True,  # 与文档切块配置的 contextual_prefix 一致
) -> list[EmbedItem]:
    """把旧构建的人工切块搬到 `new_version`，返回需要重新向量化的点。"""
    if old_version <= 0:
        return []
    old_rows = list(
        (
            await session.execute(
                select(Chunk)
                .where(
                    Chunk.app_id == app_id,
                    Chunk.document_id == document_id,
                    Chunk.index_version == old_version,
                    Chunk.source.in_(("manual", "edited")),
                )
                .order_by(Chunk.ordinal)
            )
        )
        .scalars()
        .all()
    )
    if not old_rows:
        return []
    new_rows = list(
        (
            await session.execute(
                select(Chunk)
                .where(
                    Chunk.app_id == app_id,
                    Chunk.document_id == document_id,
                    Chunk.index_version == new_version,
                )
                .order_by(Chunk.ordinal)
            )
        )
        .scalars()
        .all()
    )
    by_hash: dict[str, list[Chunk]] = {}
    for row in new_rows:
        by_hash.setdefault(text_hash(row.text), []).append(row)
    next_ordinal = max((r.ordinal for r in new_rows), default=-1) + 1
    items: list[EmbedItem] = []
    for old in old_rows:
        matched = by_hash.get(text_hash(old.text), [])
        if old.source == "edited" and matched:
            target = matched.pop(0)
            target.source = "edited"
            continue
        ordinal = next_ordinal
        next_ordinal += 1
        row = Chunk(
            id=new_id(),
            app_id=app_id,
            document_id=document_id,
            index_version=new_version,
            ordinal=ordinal,
            parent_id=None,
            heading_path=old.heading_path,
            text=old.text,
            search_text=old.search_text,
            tokens=old.tokens,
            page_from=old.page_from,
            page_to=old.page_to,
            enabled=old.enabled,
            review_status=old.review_status,
            flag_reason=old.flag_reason,
            reviewed_by=old.reviewed_by,
            reviewed_at=old.reviewed_at,
            source="manual",
        )
        session.add(row)
        items.append(
            EmbedItem(
                row.id,
                None,
                build_embed_text(
                    title,
                    old.heading_path,
                    old.text,
                    contextual_prefix=contextual_prefix,
                ),
                enabled=old.enabled and old.review_status != "flagged",
            )
        )
    return items
