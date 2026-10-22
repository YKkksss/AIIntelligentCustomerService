"""把 `chunks.kb_id` 回填到 Qdrant 的切块点 / 补充问题点（M4.7 ④ 上线脚本）。

背景：`chunks` / `chunk_questions` 在迁移 0019 里补了 `kb_id`，但历史向量点没有这个
payload；检索按 `kb_id` 过滤时（`retrieval` 的 `kb_ids`）旧点会被漏掉。本脚本按
`chunk_id` 把库归属写回 Qdrant，可重复执行（幂等）。

用法（在 `backend/` 下）：

    uv run --frozen python scripts/backfill_kb_payload.py --dry-run
    uv run --frozen python scripts/backfill_kb_payload.py
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aics.core.config import get_settings
from aics.db.models.knowledge import Chunk
from aics.vectorstore import get_vector_store

BATCH = 256


async def load_kb_map(db_url: str) -> dict[str, str]:
    """`chunk_id -> kb_id`（只取有库归属的块）。"""
    engine = create_async_engine(db_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as s:
            rows = await s.execute(select(Chunk.id, Chunk.kb_id).where(Chunk.kb_id.is_not(None)))
            return {str(cid): str(kb) for cid, kb in rows.all()}
    finally:
        await engine.dispose()


async def backfill(*, dry_run: bool) -> dict[str, int]:
    settings = get_settings()
    kb_map = await load_kb_map(settings.db_url)
    store = get_vector_store()
    if not await store.client.collection_exists(store.chunks_collection):
        return {"points": 0, "updated": 0, "missing": 0}

    by_kb: dict[tuple[str, str | None], list[UUID]] = defaultdict(list)
    seen = updated = missing = 0
    offset = None
    while True:
        points, offset = await store.client.scroll(
            store.chunks_collection,
            limit=BATCH,
            offset=offset,
            with_payload=["chunk_id", "kb_id"],
            with_vectors=False,
        )
        for point in points:
            seen += 1
            chunk_id = str((point.payload or {}).get("chunk_id") or "")
            kb_id = kb_map.get(chunk_id)
            if kb_id is None:
                missing += 1
                continue
            if (point.payload or {}).get("kb_id") == kb_id:
                continue
            by_kb[(kb_id, None)].append(UUID(str(point.id)))
        if offset is None:
            break

    if not dry_run:
        for (kb_id, _), ids in by_kb.items():
            await store.client.set_payload(
                store.chunks_collection,
                payload={"kb_id": kb_id},
                points=ids,
                wait=True,
            )
            updated += len(ids)
    else:
        updated = sum(len(v) for v in by_kb.values())

    return {"points": seen, "updated": updated, "missing": missing}


def main() -> None:
    parser = argparse.ArgumentParser(description="回填 Qdrant 切块点的 kb_id（M4.7 ④）")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写入")
    args = parser.parse_args()
    result = asyncio.run(backfill(dry_run=args.dry_run))
    print(
        f"扫描 {result['points']} 个点，{'将更新' if args.dry_run else '已更新'} "
        f"{result['updated']} 个，跳过（无库归属）{result['missing']} 个"
    )


if __name__ == "__main__":
    main()
