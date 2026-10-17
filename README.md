# AICS —— 通用知识库 AI 客服平台

AICS（AI Intelligent Customer Service）是一个面向各行各业的知识库客服平台：
每个"应用（App）"就是一个客服机器人，它服务什么领域，由上传的知识库和应用配置决定。

- **核心产品是对外 API**：每个应用独立 API Key，支持阻塞 / SSE 流式对话、引用来源、OpenAI 兼容接口、知识库管理接口。
- **管理后台**：应用管理、知识库、调试对话（看检索块、分数、改写后的问题）、评测、API Key、日志与反馈、用户权限、审计。
- **原则**：只根据知识库回答，标注出处；答不了就说不知道或转人工；准确率用内置评测量化。

## 快速导航
- 全部文档索引：[docs/README.md](docs/README.md)
- 开发计划：[docs/00-planning/03-开发计划.md](docs/00-planning/03-开发计划.md)
- 开发环境搭建：[docs/06-development/02-开发环境搭建.md](docs/06-development/02-开发环境搭建.md)
- 服务器准备：[docs/07-deployment/01-服务器准备与共享服务.md](docs/07-deployment/01-服务器准备与共享服务.md)

## 技术栈（摘要）
Python 3.12 · uv · FastAPI · LangChain + LangGraph · SQLAlchemy 2.0 async + Alembic · PostgreSQL 16 · Qdrant · Redis 8 · RabbitMQ 4 + Celery · DeepSeek（对话）· 阿里云百炼（向量化 qwen3.7-text-embedding-flash / 重排序 qwen3.7-text-rerank）· Vue 3 + Vite + TS + Element Plus

## 环境划分
| 位置 | 内容 |
|---|---|
| 开发机（Windows，本项目目录） | 后端、Worker、前端代码的编写和运行 |
| 内网服务器 192.168.110.86 | 复用 PostgreSQL 16 / Redis 8.2 / RabbitMQ 4.3 / Qdrant 1.18（共用 KnowledgeFlow 实例，集合前缀 `aics_`）；初始化脚本 `deploy/server/bootstrap/` |

## 当前状态
**M3.1（API Key 收尾）已完成**（2026-10-17，dev `a534715`）。M0–M2 已交付：基础设施与 CI、知识库入库链路（解析 / 切块 / 版本化原子切换）、混合检索与 RAG 图、对话与意图识别、对话记忆、会话生命周期、API Key 与限流；M3.1 补齐 `last_used_at` 分钟桶聚合、IP 白名单与可信代理、跨应用隔离回归集。**下一步：M3.2 知识库开放接口**。

已实现的包 / 迁移 / 接口 / 任务 / 提示词 / 测试见 [实现现状与代码地图](docs/00-handoff/02-实现现状与代码地图.md)；当前未解决的问题与等后续模块复验的项见 [待解决问题清单](docs/00-planning/07-待解决问题清单.md)；开发进度与状态总表见 [开发计划](docs/00-planning/03-开发计划.md)。
