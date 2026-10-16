# AICS 文档索引

> 所有文档使用同一套命名：应用 = `app`，知识库文档 = `document`，切块 = `chunk`，会话 = `conversation`。端口、表名、环境变量以本目录为准，改动时同步修改所有引用处。

| 目录 | 文档 | 内容 |
|---|---|---|
| 00-planning | [01-项目概述](00-planning/01-项目概述.md) | 背景、目标、成功标准 |
| | [02-范围定义](00-planning/02-范围定义.md) | v1 / v2 / 不做 |
| | [03-开发计划](00-planning/03-开发计划.md) | 阶段、里程碑、依赖、任务清单、验收 |
| | [04-风险与对策](00-planning/04-风险与对策.md) | 风险登记表 |
| | [05-术语表](00-planning/05-术语表.md) | 名词解释 |
| 01-requirements | [01-功能需求](01-requirements/01-功能需求.md) | 按模块的功能需求（FR 编号） |
| | [02-非功能需求](01-requirements/02-非功能需求.md) | 性能、可用性、安全、准确率指标 |
| 02-architecture | [01-系统架构](02-architecture/01-系统架构.md) | 部署拓扑、组件、数据流 |
| | [02-模块划分](02-architecture/02-模块划分.md) | 后端分层、模块职责、依赖图与依赖规则、chat/rag/memory 职责 |
| | [03-RAG流程设计](02-architecture/03-RAG流程设计.md) | LangGraph 节点、RunMode、RagResult、提示词版本、阈值、兜底 |
| | [04-文档处理流水线](02-architecture/04-文档处理流水线.md) | 解析、切块、版本化构建与切换、替换、重建、删除、token 口径 |
| | [05-模型供应商抽象](02-architecture/05-模型供应商抽象.md) | LLM / Embedding / Rerank 可替换 |
| | [06-技术选型](02-architecture/06-技术选型.md) | 选型与理由 |
| | [07-对话上下文管理](02-architecture/07-对话上下文管理.md) | `aics.memory`：token 预算、滚动摘要、关键事实、按需回查 |
| | [08-意图识别](02-architecture/08-意图识别.md) | 规则 + LLM 合并分类 + 向量快速通道、6 类意图与路由 |
| | [09-会话生命周期与转人工](02-architecture/09-会话生命周期与转人工.md) | 空闲提醒与关闭、转人工状态机、continue/wait 模式、坐席接口、Webhook 与会话事件流 |
| | [adr/](02-architecture/adr/) | 架构决策记录 ADR-0001 ~ 0014（0014 关键事实默认开启与版本化，0008 记忆模块与职责划分，0009 RunMode 与提示词版本化，0010 会话空闲关闭与事件推送，0011 转人工状态机与 Webhook，0012 切块预设与父子切块，0013 知识库切块与检索设置） |
| 03-database | [01-数据库设计](03-database/01-数据库设计.md) | ER 图、全部表结构、索引、DDL |
| | [02-Qdrant设计](03-database/02-Qdrant设计.md) | 集合、payload、过滤、重建 |
| | [03-迁移策略](03-database/03-迁移策略.md) | Alembic 规范 |
| 04-api | [01-接口规范](04-api/01-接口规范.md) | 版本、鉴权、错误码、分页、限流、幂等 |
| | [02-开放接口参考](04-api/02-开放接口参考.md) | 对外 API（含 SSE、OpenAI 兼容） |
| | [03-管理接口参考](04-api/03-管理接口参考.md) | 后台 API |
| | [04-第三方接入指南](04-api/04-第三方接入指南.md) | curl / Python / JS 示例、出站 Webhook 格式与签名校验 |
| 05-frontend | [01-管理后台设计](05-frontend/01-管理后台设计.md) | 页面、交互、前端工程 |
| 06-development | [01-项目目录结构](06-development/01-项目目录结构.md) | 代码目录约定 |
| | [02-开发环境搭建](06-development/02-开发环境搭建.md) | Windows + uv 从零跑起来 |
| | [03-编码规范](06-development/03-编码规范.md) | Python / TS 规范 |
| | [04-Git工作流](06-development/04-Git工作流.md) | 分支、提交、评审 |
| | [05-配置与环境变量](06-development/05-配置与环境变量.md) | 全部配置项 |
| | [06-测试策略](06-development/06-测试策略.md) | 单测、集成、E2E |
| | [07-评测指南](06-development/07-评测指南.md) | 准确率评测怎么做 |
| | [08-M1端到端测试报告](06-development/08-M1端到端测试报告.md) | M1 真实 worker 端到端与检索质量评测结果 |
| | [09-参数默认值与依据](06-development/09-参数默认值与依据.md) | 可调参数默认值的唯一来源与依据；常设规则：每轮测试后记录结果并采纳最佳值为默认 |
| 07-deployment | [01-服务器准备与共享服务](07-deployment/01-服务器准备与共享服务.md) | 建库、建用户、vhost |
| | [02-Qdrant部署](07-deployment/02-Qdrant部署.md) | compose 与验证 |
| | [03-应用部署](07-deployment/03-应用部署.md) | 后端 / Worker / 前端上线 |
| | [04-备份与恢复](07-deployment/04-备份与恢复.md) | 备份策略与演练 |
| | [05-监控与运维手册](07-deployment/05-监控与运维手册.md) | 指标、告警、故障处理 |
| 08-security | [01-安全设计](08-security/01-安全设计.md) | 密钥、隔离、注入防御、上传安全 |

## 设计讨论记录
[09-design-discussions/](09-design-discussions/README.md)：每次设计讨论（参考了谁、借鉴 / 不借鉴什么及原因、我们的补充、最终决策与落地里程碑）。**今后所有设计讨论都必须在此记录**，并在同一提交中同步相关设计文档与开发计划。
