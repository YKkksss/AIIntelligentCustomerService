# ADR-0002 向量库选用独立 Qdrant，而非 pgvector
- 状态：已接受
- 背景：服务器共享的 `db-postgres-1` 镜像未安装 pgvector；换镜像或装扩展会影响其他项目。KnowledgeFlow 项目的 Qdrant 只在其内部网络，复用会产生耦合。
- 决策：自建 `aics-qdrant`（限 1GB 内存），业务数据与切块正文仍在 PG。
- 后果：两处存储需保持一致 → 以 PG 为准，Qdrant 可随时从 PG 重建；删除走异步清理。
- 修订（2026-10-15）：服务器实际可用内存只有约 1.5GB，不再自建 `aics-qdrant`，改为**共用 KnowledgeFlow 的 Qdrant v1.18.2**（`192.168.110.86:6333`，API Key 鉴权）。隔离方式：AICS 只使用 `aics_` 前缀集合（`AICS_QDRANT_COLLECTION_PREFIX`），所有访问经 `aics.vectorstore`；仍以 PG 为准、可随时从 PG 重建，因此共用实例的故障或误删影响可恢复。代价：与 KnowledgeFlow 共享资源与升级节奏，升级需双方协调。原 `deploy/server/docker-compose.yml` 改为仅开发用的 `deploy/dev/docker-compose.yml`。
- 修订（2026-10-17）：本机开发的四个依赖服务（PG / Redis / RabbitMQ / Qdrant）统一收到根目录 `docker/` 下用 `docker compose` 管理（`docker/compose.yaml`，与服务器同版本），`deploy/dev/` 的独立 Qdrant compose 已废弃。服务器侧仍共用 KnowledgeFlow 实例，本决策不变。
