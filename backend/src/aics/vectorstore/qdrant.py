"""Qdrant 封装（docs/03-database/02-Qdrant设计.md）。

- 服务器上共用 KnowledgeFlow 的 Qdrant（ADR-0002 修订）：只操作 `prefix` 开头的集合，集合名只由本模块生成。
- 所有读写都**强制** `app_id`：方法第一个参数是 app_id，过滤条件由本模块拼装，不提供无过滤的检索方法。
- 正文与标题不进 Qdrant，payload 只有 app_id / document_id / chunk_id / index_version / enabled，
  以及 M2.2b 起的 `kind`（chunk / question，缺省视为 chunk）与 `tags`（文档标签，过滤检索）。
- Supplementary-question points (M2.2b) live in the same collection: point id = question id,
  `chunk_id` = the chunk they answer, so chunk-level operations filter on payload `chunk_id`
  and document-level ones on `document_id` and cover both kinds; counts exclude questions.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

import structlog
from qdrant_client import AsyncQdrantClient
from qdrant_client import models as qm

PointKind = Literal["chunk", "question"]
CollectionKind = Literal["chunks", "messages", "intents"]
SEARCH_HNSW_EF = 128

log = structlog.get_logger()


FACET_LIMIT = 100_000  # 单应用文档数上限远小于此


class VectorStoreError(Exception):
    pass


def sanitize(name: str) -> str:
    return re.sub(r"[^0-9a-z]", "_", name.lower())


def collection_name(prefix: str, kind: CollectionKind, embed_model: str, dim: int) -> str:
    """`{prefix}{kind}__{model}__{dim}`，例：aics_chunks__qwen3_7_text_embedding_flash__1024。"""
    if not prefix.startswith("aics_"):
        raise VectorStoreError(f"集合前缀必须以 aics_ 开头：{prefix!r}")
    if dim < 1:
        raise VectorStoreError("dim 必须 ≥ 1")
    return f"{prefix}{kind}__{sanitize(embed_model)}__{dim}"


@dataclass(frozen=True)
class ChunkPoint:
    chunk_id: UUID
    document_id: UUID
    index_version: int
    vector: Sequence[float]
    enabled: bool = True
    parent_id: UUID | None = None  # 父子模式：子块所属父块
    tags: Sequence[str] = ()
    kind: PointKind = "chunk"
    point_id: UUID | None = None  # question points: the question id (defaults to chunk_id)
    kb_id: UUID | None = None  # 知识库归属（M4.7 ④）：检索按库集合过滤


@dataclass(frozen=True)
class ChunkHit:
    chunk_id: UUID  # always the chunk (for question points: the chunk the question maps to)
    document_id: UUID
    index_version: int
    score: float
    kind: PointKind = "chunk"
    point_id: UUID | None = None


def _is_question() -> qm.FieldCondition:
    return qm.FieldCondition(key="kind", match=qm.MatchValue(value="question"))


def _tags(tags: Sequence[str]) -> qm.FieldCondition:
    return qm.FieldCondition(key="tags", match=qm.MatchAny(any=list(tags)))


def _app(app_id: UUID) -> qm.FieldCondition:
    return qm.FieldCondition(key="app_id", match=qm.MatchValue(value=str(app_id)))


def _kbs(kb_ids: Sequence[UUID]) -> qm.FieldCondition:
    return qm.FieldCondition(key="kb_id", match=qm.MatchAny(any=[str(k) for k in kb_ids]))


class QdrantVectorStore:
    def __init__(
        self, client: AsyncQdrantClient, *, prefix: str, embed_model: str, dim: int
    ) -> None:
        self.client = client
        self.prefix = prefix
        self.dim = dim
        self.chunks_collection = collection_name(prefix, "chunks", embed_model, dim)
        self._ensured: set[str] = set()

    async def aclose(self) -> None:
        await self.client.close()

    # ---------------- 集合 ----------------
    async def ensure_chunks_collection(self) -> None:
        """不存在则创建（参数见 Qdrant 设计 §1）；已存在则校验维度。可重复调用。"""
        name = self.chunks_collection
        if name in self._ensured:
            return
        if await self.client.collection_exists(name):
            info = await self.client.get_collection(name)
            vectors = info.config.params.vectors
            size = vectors.size if isinstance(vectors, qm.VectorParams) else None
            if size != self.dim:
                raise VectorStoreError(f"集合 {name} 维度为 {size}，配置为 {self.dim}")
        else:
            await self.client.create_collection(
                name,
                vectors_config=qm.VectorParams(
                    size=self.dim, distance=qm.Distance.COSINE, on_disk=True
                ),
                hnsw_config=qm.HnswConfigDiff(m=16, ef_construct=128, payload_m=16),
                quantization_config=qm.ScalarQuantization(
                    scalar=qm.ScalarQuantizationConfig(type=qm.ScalarType.INT8, always_ram=True)
                ),
                on_disk_payload=True,
            )
            for field, schema in (
                ("app_id", qm.KeywordIndexParams(type=qm.KeywordIndexType.KEYWORD, is_tenant=True)),
                ("kb_id", qm.PayloadSchemaType.KEYWORD),
                ("document_id", qm.PayloadSchemaType.KEYWORD),
                ("index_version", qm.PayloadSchemaType.INTEGER),
                ("enabled", qm.PayloadSchemaType.BOOL),
            ):
                await self.client.create_payload_index(name, field, field_schema=schema)
            log.info("vectorstore.collection_created", collection=name, dim=self.dim)
        # M2.2b indexes; create_payload_index is idempotent, so pre-M2.2b collections get them too
        for field in ("chunk_id", "kind", "tags"):
            await self.client.create_payload_index(
                name, field, field_schema=qm.PayloadSchemaType.KEYWORD
            )
        self._ensured.add(name)

    async def drop_chunks_collection(self) -> None:
        """仅用于换模型后清理旧集合与测试；名字由本模块生成，不会误删其他项目的集合。"""
        await self.client.delete_collection(self.chunks_collection)
        self._ensured.discard(self.chunks_collection)

    # ---------------- 写入 ----------------
    async def upsert_chunks(self, app_id: UUID, points: Sequence[ChunkPoint]) -> int:
        if not points:
            return 0
        await self.ensure_chunks_collection()
        structs = []
        for p in points:
            if len(p.vector) != self.dim:
                raise VectorStoreError(f"向量维度 {len(p.vector)} ≠ {self.dim}")
            structs.append(
                qm.PointStruct(
                    id=str(p.point_id or p.chunk_id),
                    vector=list(p.vector),
                    payload={
                        "app_id": str(app_id),
                        "document_id": str(p.document_id),
                        "chunk_id": str(p.chunk_id),
                        "index_version": p.index_version,
                        "enabled": p.enabled,
                        "kind": p.kind,
                        "tags": list(p.tags),
                        **({"parent_id": str(p.parent_id)} if p.parent_id else {}),
                    },
                )
            )
        await self.client.upsert(self.chunks_collection, points=structs, wait=True)
        return len(structs)

    async def set_enabled(self, app_id: UUID, chunk_ids: Sequence[UUID], enabled: bool) -> None:
        """同步 PG 中 `enabled AND review_status <> 'flagged'` 的变化；只作用于本应用的点。

        Filters on payload `chunk_id`, so the chunk's supplementary-question points follow."""
        if not chunk_ids:
            return
        await self.ensure_chunks_collection()
        await self.client.set_payload(
            self.chunks_collection,
            payload={"enabled": enabled},
            points=qm.Filter(
                must=[
                    _app(app_id),
                    qm.FieldCondition(
                        key="chunk_id", match=qm.MatchAny(any=[str(c) for c in chunk_ids])
                    ),
                ]
            ),
            wait=True,
        )

    async def set_document_tags(self, app_id: UUID, document_id: UUID, tags: Sequence[str]) -> None:
        """Mirror `documents.tags` onto every point of the document (all builds, both kinds)."""
        if not await self.client.collection_exists(self.chunks_collection):
            return
        await self.client.set_payload(
            self.chunks_collection,
            payload={"tags": list(tags)},
            points=qm.Filter(
                must=[
                    _app(app_id),
                    qm.FieldCondition(
                        key="document_id", match=qm.MatchValue(value=str(document_id))
                    ),
                ]
            ),
            wait=True,
        )

    async def move_question(
        self,
        app_id: UUID,
        question_id: UUID,
        *,
        chunk_id: UUID,
        index_version: int,
        enabled: bool,
        parent_id: UUID | None,
    ) -> None:
        """Re-point a question to a new build's chunk without re-embedding (payload only)."""
        await self.ensure_chunks_collection()
        payload: dict[str, object] = {
            "chunk_id": str(chunk_id),
            "index_version": index_version,
            "enabled": enabled,
        }
        if parent_id is not None:
            payload["parent_id"] = str(parent_id)
        await self.client.set_payload(
            self.chunks_collection,
            payload=payload,
            points=qm.Filter(must=[_app(app_id), qm.HasIdCondition(has_id=[str(question_id)])]),
            wait=True,
        )

    async def delete_points(self, app_id: UUID, point_ids: Sequence[UUID]) -> None:
        """Delete specific points of the app (supplementary questions)."""
        if not point_ids or not await self.client.collection_exists(self.chunks_collection):
            return
        await self.client.delete(
            self.chunks_collection,
            points_selector=qm.FilterSelector(
                filter=qm.Filter(
                    must=[_app(app_id), qm.HasIdCondition(has_id=[str(p) for p in point_ids])]
                )
            ),
            wait=True,
        )

    async def set_document_enabled(self, app_id: UUID, document_id: UUID, enabled: bool) -> None:
        """整篇文档的点（全部构建号）改 enabled：删除文档时先让其立即不可检索。"""
        if not await self.client.collection_exists(self.chunks_collection):
            return
        await self.client.set_payload(
            self.chunks_collection,
            payload={"enabled": enabled},
            points=qm.Filter(
                must=[
                    _app(app_id),
                    qm.FieldCondition(
                        key="document_id", match=qm.MatchValue(value=str(document_id))
                    ),
                ]
            ),
            wait=True,
        )

    # ---------------- 读取 ----------------
    async def search_chunks(
        self,
        app_id: UUID | None,
        vector: Sequence[float],
        limit: int,
        *,
        enabled_only: bool = True,
        tags: Sequence[str] | None = None,
        include_questions: bool = True,
        kb_ids: Sequence[UUID] | None = None,
    ) -> list[ChunkHit]:
        """Nearest points of the app; question points map to their chunk (callers dedup).

        `tags` = any-match filter on document tags (None / empty = no filter)."""
        if len(vector) != self.dim:
            raise VectorStoreError(f"查询向量维度 {len(vector)} ≠ {self.dim}")
        if not await self.client.collection_exists(self.chunks_collection):
            return []
        # M4.7 ④：有库集合时按 kb_id 过滤（文档可没有 app_id），否则退回按应用过滤
        must: list[qm.Condition] = [
            _kbs(kb_ids) if kb_ids else _app(app_id)  # type: ignore[arg-type]
        ]
        if enabled_only:
            must.append(qm.FieldCondition(key="enabled", match=qm.MatchValue(value=True)))
        if tags:
            must.append(_tags(tags))
        must_not = [] if include_questions else [_is_question()]
        resp = await self.client.query_points(
            self.chunks_collection,
            query=list(vector),
            limit=limit,
            query_filter=qm.Filter(must=must, must_not=must_not),
            search_params=qm.SearchParams(
                hnsw_ef=SEARCH_HNSW_EF, quantization=qm.QuantizationSearchParams(rescore=True)
            ),
            with_payload=True,
        )
        hits = []
        for pt in resp.points:
            pl = pt.payload or {}
            if pl.get("app_id") != str(app_id):  # 防御：过滤失效时也不越权返回
                continue
            kind: PointKind = "question" if pl.get("kind") == "question" else "chunk"
            hits.append(
                ChunkHit(
                    chunk_id=UUID(str(pl.get("chunk_id") or pt.id)),
                    document_id=UUID(pl["document_id"]),
                    index_version=int(pl["index_version"]),
                    score=float(pt.score),
                    kind=kind,
                    point_id=UUID(str(pt.id)),
                )
            )
        return hits

    async def count_chunks(
        self,
        app_id: UUID,
        *,
        enabled_only: bool = True,
        kb_ids: Sequence[UUID] | None = None,
    ) -> int:
        if not await self.client.collection_exists(self.chunks_collection):
            return 0
        # M4.7 ④：有库集合时按 kb_id 过滤（文档可没有 app_id），否则退回按应用过滤
        must: list[qm.Condition] = [
            _kbs(kb_ids) if kb_ids else _app(app_id)  # type: ignore[arg-type]
        ]
        if enabled_only:
            must.append(qm.FieldCondition(key="enabled", match=qm.MatchValue(value=True)))
        res = await self.client.count(
            self.chunks_collection,
            count_filter=qm.Filter(must=must, must_not=[_is_question()]),
            exact=True,
        )
        return res.count

    async def count_by_document(
        self,
        app_id: UUID,
        *,
        enabled_only: bool = True,
        limit: int = FACET_LIMIT,
        kb_ids: Sequence[UUID] | None = None,
    ) -> dict[UUID, int]:
        """按 document_id 精确分组计数（Qdrant facet，exact=True）；对账与孤儿点清理用。"""
        if not await self.client.collection_exists(self.chunks_collection):
            return {}
        # M4.7 ④：有库集合时按 kb_id 过滤（文档可没有 app_id），否则退回按应用过滤
        must: list[qm.Condition] = [
            _kbs(kb_ids) if kb_ids else _app(app_id)  # type: ignore[arg-type]
        ]
        if enabled_only:
            must.append(qm.FieldCondition(key="enabled", match=qm.MatchValue(value=True)))
        res = await self.client.facet(
            self.chunks_collection,
            key="document_id",
            facet_filter=qm.Filter(must=must, must_not=[_is_question()]),
            limit=limit,
            exact=True,
        )
        return {UUID(str(h.value)): int(h.count) for h in res.hits}

    # ---------------- 删除 ----------------
    async def delete_document(
        self,
        app_id: UUID,
        document_id: UUID,
        *,
        index_version: int | None = None,
        below_index_version: int | None = None,
    ) -> None:
        """删除文档的点：全部 / 某个构建号（清半成品）/ 低于某构建号（清旧版本）。"""
        if index_version is not None and below_index_version is not None:
            raise ValueError("index_version 与 below_index_version 只能二选一")
        if not await self.client.collection_exists(self.chunks_collection):
            return
        must: list[qm.Condition] = [
            _app(app_id),
            qm.FieldCondition(key="document_id", match=qm.MatchValue(value=str(document_id))),
        ]
        if index_version is not None:
            must.append(
                qm.FieldCondition(key="index_version", match=qm.MatchValue(value=index_version))
            )
        if below_index_version is not None:
            must.append(
                qm.FieldCondition(key="index_version", range=qm.Range(lt=below_index_version))
            )
        await self.client.delete(
            self.chunks_collection,
            points_selector=qm.FilterSelector(filter=qm.Filter(must=must)),
            wait=True,
        )

    async def delete_app(self, app_id: UUID) -> None:
        """应用删除（cleanup_app）：删除该应用的全部点。"""
        if not await self.client.collection_exists(self.chunks_collection):
            return
        await self.client.delete(
            self.chunks_collection,
            points_selector=qm.FilterSelector(filter=qm.Filter(must=[_app(app_id)])),
            wait=True,
        )
