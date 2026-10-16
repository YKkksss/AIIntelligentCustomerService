# Qdrant 设计

## 1. 集合
- 实例：共用 KnowledgeFlow 的 Qdrant v1.18.2（ADR-0002 修订），AICS 只操作以 `AICS_QDRANT_COLLECTION_PREFIX`（默认 `aics_`，集成测试 `aics_test_`）开头的集合。
- 命名：`{prefix}chunks__{embed_model}__{dim}`，模型名中非字母数字字符替换为 `_`，例：`aics_chunks__qwen3_7_text_embedding_flash__1024`。
- 一个平台一个活动集合，所有应用共用，用 payload `app_id` 隔离（多租户推荐做法）。
- 参数：
```json
{
  "vectors": {"size": 1024, "distance": "Cosine", "on_disk": true},
  "hnsw_config": {"m": 16, "ef_construct": 128, "payload_m": 16},
  "quantization_config": {"scalar": {"type": "int8", "always_ram": true}},
  "on_disk_payload": true
}
```
int8 量化 + 原向量放磁盘，内存约为原来的 1/4：100 万 × 1024 维 ≈ 1GB 原始 → 常驻约 0.3GB。

## 2. Point
- `id` = `chunks.id`（UUID）。
- payload：`{"app_id": "...", "document_id": "...", "chunk_id": "...", "index_version": 3, "enabled": true}`（正文、文档标题都不存 Qdrant，以 PG 为准）。父子模式下点对应子块，payload 额外带 `parent_id`（检索后在 PG 侧按其去重；不建索引）。`enabled` = PG 中 `enabled AND review_status <> 'flagged'`，禁用 / 复核状态变化时同步 `set_payload`。
- payload 索引：`app_id`（keyword, `is_tenant: true`）、`document_id`（keyword）、`index_version`（integer）、`enabled`（bool）。
- 同一文档可能短暂同时存在新旧两个构建号的点（替换 / 重建期间）；检索不按版本过滤，向量命中回 PG 取正文时按[数据库设计 §3.7](01-数据库设计.md)的可检索条件过滤，旧版本点由 `cleanup_document` 删除。为抵消过滤损耗，向量检索 `limit = vector_top_k × 1.5`，过滤后取前 `vector_top_k`。

## 3. 检索
```python
client.query_points(collection, query=vec, limit=k,
    query_filter=Filter(must=[FieldCondition(key="app_id", match=MatchValue(value=app_id)),
                              FieldCondition(key="enabled", match=MatchValue(value=True))]),
    search_params=SearchParams(hnsw_ef=128, quantization=QuantizationSearchParams(rescore=True)))
```
检索封装层**强制要求** `app_id` 参数，不提供无过滤的检索方法。会话消息集合 `aics_messages__*` 与意图示例集合 `aics_intents__*` 同理（分别强制 `app_id + conversation_id`、`app_id`）。

## 4. 换模型 / 重建
1. 新配置模型 → 创建新集合（新名字）。
2. `reindex_all` 任务（队列 `ingest`）：遍历所有有生效版本的文档，读 PG 中生效构建号的 chunks 重新向量化写入新集合（无需重新解析，不改构建号）。
3. 完成后切换配置并重启；旧集合保留 7 天后删除。

## 5. 一致性
PG 为准。每日 beat 任务 `reconcile`（队列 `maintenance`）：按应用比较 PG 可检索切块数（生效版本、`enabled AND review_status <> 'flagged'`）与 Qdrant `count(filter app_id, enabled=true)`；存在进行中构建或待清理旧版本的文档跳过不比较，差异写日志与指标，可一键对单个应用重建；手动执行时写 `jobs(type=reconcile)`。
