# Qdrant 设计

## 1. 集合
- 命名：`aics_chunks__{embed_model}__{dim}`，非字母数字字符替换为 `_`，例：`aics_chunks__text_embedding_v4__1024`。
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
- payload：`{"app_id": "...", "document_id": "...", "chunk_id": "...", "enabled": true}`（正文不存 Qdrant，以 PG 为准）。
- payload 索引：`app_id`（keyword, `is_tenant: true`）、`document_id`（keyword）、`enabled`（bool）。

## 3. 检索
```python
client.query_points(collection, query=vec, limit=k,
    query_filter=Filter(must=[FieldCondition(key="app_id", match=MatchValue(value=app_id)),
                              FieldCondition(key="enabled", match=MatchValue(value=True))]),
    search_params=SearchParams(hnsw_ef=128, quantization=QuantizationSearchParams(rescore=True)))
```
检索封装层**强制要求** `app_id` 参数，不提供无过滤的检索方法。

## 4. 换模型 / 重建
1. 新配置模型 → 创建新集合（新名字）。
2. `reindex_all` 任务：遍历所有 done 文档，读 PG chunks 重新向量化写入新集合（无需重新解析）。
3. 完成后切换配置并重启；旧集合保留 7 天后删除。

## 5. 一致性
PG 为准。每日 `maintenance.reconcile`：按应用比较 PG enabled chunk 数与 Qdrant `count(filter app_id)`，差异写日志并告警，可一键对单个应用重建。
