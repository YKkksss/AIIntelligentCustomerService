# AICS 文档索引

> 所有文档使用同一套命名：应用 = `app`，知识库文档 = `document`，切块 = `chunk`，会话 = `conversation`。端口、表名、环境变量以本目录为准，改动时同步修改所有引用处。

| 目录 | 文档 | 内容 |
|---|---|---|
| 00-planning | [01-项目概述](00-planning/01-项目概述.md) | 背景、目标、成功标准 |
| | [02-范围定义](00-planning/02-范围定义.md) | v1 / v2 / 不做 |
| | [03-开发计划](00-planning/03-开发计划.md) | 阶段、里程碑、任务清单、验收、估时 |
| | [04-风险与对策](00-planning/04-风险与对策.md) | 风险登记表 |
| | [05-术语表](00-planning/05-术语表.md) | 名词解释 |
| 01-requirements | [01-功能需求](01-requirements/01-功能需求.md) | 按模块的功能需求（FR 编号） |
| | [02-非功能需求](01-requirements/02-非功能需求.md) | 性能、可用性、安全、准确率指标 |
| 02-architecture | [01-系统架构](02-architecture/01-系统架构.md) | 部署拓扑、组件、数据流 |
| | [02-模块划分](02-architecture/02-模块划分.md) | 后端分层与模块职责 |
| | [03-RAG流程设计](02-architecture/03-RAG流程设计.md) | LangGraph 节点、状态、提示词、阈值、兜底 |
| | [04-文档处理流水线](02-architecture/04-文档处理流水线.md) | 解析、切块、索引、重建 |
| | [05-模型供应商抽象](02-architecture/05-模型供应商抽象.md) | LLM / Embedding / Rerank 可替换 |
| | [06-技术选型](02-architecture/06-技术选型.md) | 选型与理由 |
| | [adr/](02-architecture/adr/) | 架构决策记录 ADR-0001 ~ 0005 |
| 03-database | [01-数据库设计](03-database/01-数据库设计.md) | ER 图、全部表结构、索引、DDL |
| | [02-Qdrant设计](03-database/02-Qdrant设计.md) | 集合、payload、过滤、重建 |
| | [03-迁移策略](03-database/03-迁移策略.md) | Alembic 规范 |
| 04-api | [01-接口规范](04-api/01-接口规范.md) | 版本、鉴权、错误码、分页、限流、幂等 |
| | [02-开放接口参考](04-api/02-开放接口参考.md) | 对外 API（含 SSE、OpenAI 兼容） |
| | [03-管理接口参考](04-api/03-管理接口参考.md) | 后台 API |
| | [04-第三方接入指南](04-api/04-第三方接入指南.md) | curl / Python / JS 示例 |
| 05-frontend | [01-管理后台设计](05-frontend/01-管理后台设计.md) | 页面、交互、前端工程 |
| 06-development | [01-项目目录结构](06-development/01-项目目录结构.md) | 代码目录约定 |
| | [02-开发环境搭建](06-development/02-开发环境搭建.md) | Windows + uv 从零跑起来 |
| | [03-编码规范](06-development/03-编码规范.md) | Python / TS 规范 |
| | [04-Git工作流](06-development/04-Git工作流.md) | 分支、提交、评审 |
| | [05-配置与环境变量](06-development/05-配置与环境变量.md) | 全部配置项 |
| | [06-测试策略](06-development/06-测试策略.md) | 单测、集成、E2E |
| | [07-评测指南](06-development/07-评测指南.md) | 准确率评测怎么做 |
| 07-deployment | [01-服务器准备与共享服务](07-deployment/01-服务器准备与共享服务.md) | 建库、建用户、vhost |
| | [02-Qdrant部署](07-deployment/02-Qdrant部署.md) | compose 与验证 |
| | [03-应用部署](07-deployment/03-应用部署.md) | 后端 / Worker / 前端上线 |
| | [04-备份与恢复](07-deployment/04-备份与恢复.md) | 备份策略与演练 |
| | [05-监控与运维手册](07-deployment/05-监控与运维手册.md) | 指标、告警、故障处理 |
| 08-security | [01-安全设计](08-security/01-安全设计.md) | 密钥、隔离、注入防御、上传安全 |
