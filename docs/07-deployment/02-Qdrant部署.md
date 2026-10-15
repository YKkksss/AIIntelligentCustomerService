# Qdrant（共用 KnowledgeFlow 实例）

> 2026-10-15 起 AICS 不再部署自己的 Qdrant，改为共用 KnowledgeFlow 的实例（ADR-0002 修订）。原 `deploy/server/docker-compose.yml` 已移除；本机开发需要独立 Qdrant 时用 `deploy/dev/docker-compose.yml`（仅开发用）。

## 1. 连接
| 项 | 值 |
|---|---|
| 版本 | `qdrant/qdrant:v1.18.2`（KnowledgeFlow 维护） |
| 地址 | `http://192.168.110.86:6333`（`AICS_QDRANT_URL`） |
| 鉴权 | API Key（`AICS_QDRANT_API_KEY`，向负责人获取，只写进 `.env`） |
| 集合前缀 | `aics_`（`AICS_QDRANT_COLLECTION_PREFIX`；集成测试 `aics_test_`） |
| 客户端 | `qdrant-client` 1.15+（异步客户端 `AsyncQdrantClient`），与服务端 1.18 兼容 |

## 2. 验证
```bash
curl -s -H "api-key: <QDRANT_API_KEY>" http://192.168.110.86:6333/readyz
curl -s -H "api-key: <QDRANT_API_KEY>" http://192.168.110.86:6333/collections | jq '.result.collections[].name | select(startswith("aics_"))'
```
集合由 `aics.vectorstore` 在首次写入前自动创建（`ensure_collection`，见 Qdrant 设计），无需手工建。

## 3. 共用约束
- 只操作 `aics_` 前缀集合：创建、写入、删除、快照都经 `aics.vectorstore`，集合名由其统一生成；不对实例做全局配置修改、不重启容器、不升级版本（由 KnowledgeFlow 负责人安排；升级前通知 AICS 先做快照）。
- 资源：AICS 集合用 int8 量化 + 原向量放磁盘，常驻内存约为原始向量的 1/4；服务器可用内存约 1.5GB，切块量较大前与负责人确认。
- 备份：只对 AICS 集合做快照（见[备份与恢复](04-备份与恢复.md)）；最可靠的恢复方式是从 PG 重建。

## 4. 本机开发（可选）
`docker compose -f deploy/dev/docker-compose.yml up -d` 启动本机 Qdrant（同版本 v1.18.2，API Key 取 `deploy/dev/.env`），`.env` 中 `AICS_QDRANT_URL=http://localhost:6333`。
