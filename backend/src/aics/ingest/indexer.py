"""向量化文本与写入 Qdrant（文档处理流水线 §3 第 6–7 步）。

`build_embed_text` 是向量化文本的**唯一**生成函数：入库、单切块重新向量化（M4.3b）、预览展示都调用它，
因此不单独存储向量化文本；切块正文 `chunks.text` 始终是原文。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from uuid import UUID

from aics.providers.base import EmbeddingProvider
from aics.vectorstore import ChunkPoint, QdrantVectorStore

TITLE_LABEL = "文档："
SECTION_LABEL = "章节："


def build_embed_text(
    title: str | None, heading_path: str | None, text: str, *, contextual_prefix: bool = True
) -> str:
    """contextual_prefix=true：`文档：{title}\\n章节：{heading_path}\\n{text}`（空行省略）；否则 `{heading_path}\\n{text}`。"""
    if contextual_prefix:
        head = [f"{TITLE_LABEL}{title.strip()}"] if title and title.strip() else []
        if heading_path:
            head.append(f"{SECTION_LABEL}{heading_path}")
        return "\n".join([*head, text])
    return f"{heading_path}\n{text}" if heading_path else text


@dataclass(frozen=True)
class EmbedItem:
    chunk_id: UUID
    parent_id: UUID | None
    text: str  # 已由 build_embed_text 生成
    enabled: bool = True


async def embed_and_upsert(
    *,
    app_id: UUID,
    document_id: UUID,
    index_version: int,
    items: Sequence[EmbedItem],
    embedder: EmbeddingProvider,
    store: QdrantVectorStore,
    batch_size: int,
    before_batch: Callable[[int, int], Awaitable[None]] | None = None,
) -> int:
    """分批向量化并 upsert；每批之前调用 `before_batch(done, total)`（中止检查与进度更新）。"""
    done = 0
    for start in range(0, len(items), max(1, batch_size)):
        if before_batch is not None:
            await before_batch(done, len(items))
        batch = items[start : start + batch_size]
        vectors = await embedder.embed_documents([i.text for i in batch])
        await store.upsert_chunks(
            app_id,
            [
                ChunkPoint(i.chunk_id, document_id, index_version, v, i.enabled, i.parent_id)
                for i, v in zip(batch, vectors, strict=True)
            ],
        )
        done += len(batch)
    return done
