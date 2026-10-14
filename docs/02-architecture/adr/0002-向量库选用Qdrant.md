# ADR-0002 向量库选用独立 Qdrant，而非 pgvector
- 状态：已接受
- 背景：服务器共享的 `db-postgres-1` 镜像未安装 pgvector；换镜像或装扩展会影响其他项目。KnowledgeFlow 项目的 Qdrant 只在其内部网络，复用会产生耦合。
- 决策：自建 `aics-qdrant`（限 1GB 内存），业务数据与切块正文仍在 PG。
- 后果：两处存储需保持一致 → 以 PG 为准，Qdrant 可随时从 PG 重建；删除走异步清理。
