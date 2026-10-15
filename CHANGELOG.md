# 更新日志（CHANGELOG）

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 新增（M1.1 外部依赖封装，2026-10-16）
- `vectorstore/qdrant.py`：共享 Qdrant 集合命名（前缀须 `aics_`）、建集合（on_disk + int8 量化 + payload 索引，`app_id` 租户索引）、所有读写强制 `app_id`；`qdrant-client` 兼容 1.18。
- `storage/`：`Storage` 协议与 `LocalStorage`（原子写、路径穿越校验）。
- `providers/`：Embedding/Rerank/LLM 接口，OpenAI 兼容向量化实现（批量、维度校验），超时/重试/熔断。
- 测试：单元（含 Redis ACL 禁用命令扫描）、共享 Qdrant 集成（`AICS_TEST_QDRANT_URL`/`AICS_TEST_QDRANT_API_KEY`）、live 向量化（`AICS_LIVE_EMBED_API_KEY`）。

### 变更（服务器实际情况，2026-10-16）
- 服务器可用内存实际约 1.5GB：不再自建 Qdrant，共用 KnowledgeFlow 的 Qdrant v1.18.2（API Key，集合前缀 `aics_`，ADR-0002 修订）；`deploy/server/docker-compose.yml` 改为仅本机开发用的 `deploy/dev/docker-compose.yml`；应用容器内存上限按 1.5GB 预算下调；备份只快照 `aics_` 集合。
- 服务器已就绪：PG 16.15 库 `aics`/`aics_test` 与角色 `aics`；RabbitMQ 4.3.5 vhost/用户 `aics`；Redis 8.2.9 ACL 用户 `aics`（`~aics:* ~celery-task-meta-* ~chord-unlock-* &aics:* &celery-task-meta-*`，无 `@admin`/`@dangerous`，写在 `/etc/shared-infra/redis.conf`）；bootstrap 脚本与文档同步。
- 开发计划：M1–M6 拆为子步骤（M1.1…M6.5），每步给出交付物、验收与依赖。

### 新增（M0 基础设施，2026-10-15）
- 后端（`backend/`，Python 3.12 + uv）：FastAPI app factory（统一错误响应、`X-Request-ID`、structlog JSON 日志、prod 关闭 `/docs`）；`/healthz`；`/readyz` 并发检查 PostgreSQL / Redis / RabbitMQ / Qdrant（单项 2 秒超时，失败 503，不暴露错误细节）；console script `aics-api`、`aics-cli`（骨架）。
- 配置：pydantic-settings（`AICS_` 前缀，根目录与 `backend/.env`），`AICS_MODELS` 注册表校验（名称唯一、kind、窗口、价格），`AICS_LLM_MODEL` / `AICS_EMBED_MODEL` / `AICS_RERANK_MODEL` 必须已注册且类型匹配；JWT 密钥 ≥ 64、pepper ≥ 32 字符。
- core：`tokens.count_tokens`（cl100k_base）、`textsearch`（按应用 jieba `Tokenizer` LRU 缓存、自定义词典不污染全局、`build_tsquery` 清洗 + OR 连接）、`prompts`（front-matter 加载、`prompts.lock.json` 版本检查 `python -m aics.core.prompts check|update`）、`models`、`errors`、`logging`。
- db：async SQLAlchemy 引擎/会话、UUIDv7 主键、约束命名约定、`AppScopedRepository`（签名校验工具）；Alembic（async）与迁移 `0001`：`users`、`roles`（3 个角色种子权限）。
- Celery app：队列 `ingest` / `context` / `eval` / `maintenance`（`acks_late`、prefetch 1），beat 表待后续里程碑填充。
- 质量：ruff、import-linter（13 条合同，对应模块划分 §3）、pytest（单元 76 项，覆盖率 95%；迁移往返集成测试）、pre-commit（含 gitleaks、import-linter、提示词检查）、GitHub Actions CI（gitleaks、后端、前端）。
- 前端（`frontend/`）：Vite + Vue 3 + TS + Element Plus + Vue Router + Pinia + axios，登录页占位、`/readyz` 状态展示；ESLint + Prettier。
- 部署：`deploy/server/bootstrap/`（PG 库与用户 SQL、RabbitMQ vhost、Redis ACL、目录与检查，可重复执行）；`scripts/dev.ps1`。

### 文档（M0 同步）
- 目录结构补充 `health.py`、`prompts.lock.json`、`deploy/server/bootstrap/`；开发环境改用 `uv run aics-api`；配置文档补 `AICS_DEBUG` 与 `.env` 读取顺序；users 表补 NOT NULL / 默认值与小写约束，`user_app_roles` 移到 M1；`.gitattributes` 改为 `eol=lf` 与编码规范一致。

### 变更（v0.2 文档修订，2026-10-15）
- 模块边界：新增 `aics.memory`（上下文组装、压缩、事实、回查）、`aics.rag.intent`（意图纯函数）、`aics.modules.stats`；`rag` 通过 `rag.ports` 依赖倒置，不直接访问数据库；写出模块依赖图与依赖规则；`AppRagConfig` 唯一定义在 `aics/rag/config.py`；新增 `core.tokens`、`core.textsearch`、`core.models`、`core.prompts`、`AppScopedRepository`。
- 职责划分：`chat.service` 统一编排对话（落库顺序、`refusal_streak`、`handoff`、压缩触发），调试 / 评测 / OpenAI 兼容复用；rag 图只返回 `RagResult`，入口带 `RunMode`（online/debug/eval）。新增 ADR-0008、ADR-0009。
- 数据模型：chunks 增加 `index_version`、`review_status`；documents 增加 `version`、`pending_index_version`、`chunk_config_version`，`apps.index_version` 更名为 `chunk_config_version`；messages 增加 `search_text`/`tsv`、`prompt_version`；conversations 增加 `status`、`closed_at`、`handoff_at/reason`，`channel` 增加 `eval`；新表 `app_daily_stats`；jobs 类型与 `cancelled` 状态补全；apps/documents 状态增加 `deleting`；feedback 关联评测用例。
- 会话生命周期（ADR-0010）：按应用配置空闲关闭（`session_config.idle_close_minutes` 默认 10 分钟，平台默认在 `system_settings`），关闭前 1 分钟发提醒（`idle_close_notice`）；beat `idle_sweep` 每 30 秒执行；向已关闭会话发消息自动新建并返回 `previous_conversation_id`；消息角色增加 `human_agent`、`system`（`kind`）；新增会话事件流 SSE。
- 转人工（ADR-0011）：`handoff_status` 状态机（none/pending/accepted/released）取代布尔 `handoff`；`handoff_ai_mode` continue/wait；固定格式出站 Webhook（HMAC 签名、6 次退避重试、`webhook_deliveries`、重发）；坐席入站接口（scope `handoff:write`）；新增模块 `handoff`、`webhooks`；统计增加转人工与空闲关闭指标；响应增加 `reply_by`、`handoff_status`。
- 转人工超时与留言（ADR-0011 第 6 条）：排队超时 `handoff_queue_timeout_minutes`（默认 5）自动退出排队、发话术、回到 AI、标记待回访、事件 `handoff.queue_timeout`；可选服务时间 `service_hours`（周计划 + 时区，未配置 = 7×24），非服务时间直接转留言（`offline_text`、待回访、事件 `handoff.offline`）；接入后双方无消息 `handoff_accepted_idle_minutes`（默认 30）自动结束人工并关闭会话；不新增状态，原因记 `handoff_end_reason`；待回访列表与清除接口/页面；统计新增 `handoff_queue_timeout`、`handoff_offline`、`handoff_accepted_idle_closed`；后台强制释放保留。均由 `idle_sweep` 执行。
- 开发计划改为里程碑 + 交付物 + 依赖，移除全部估时。
- 行为定义：文档替换（同 ID、版本化原子切换、哈希规则）；rerank 失败与 judge 失败的唯一规则；citations 事件仅在判定可答后发送；关键词检索统一 OR tsquery；槽位键统一 `order_no`；`intent_source` 精确定义；`answer_type` 统一四值；引用字段统一 `document_title`/`page`；RBAC 权限与 API Key scope 分开命名；OpenAI 兼容 `X-AICS-Conversation-Id`；移除 `history_turns`；校验错误统一 422。
- 配置：新增 `AICS_MODELS`、`AICS_DATA_ENCRYPTION_KEY`、`AICS_URL_FETCH_ALLOWLIST`、`AICS_MESSAGE_RETENTION_DAYS`、`AICS_TRACE_RETENTION_DAYS`、`AICS_MESSAGE_EMBED_ENABLED`；Celery 队列 `context` 写入 ADR-0003、开发与部署 worker 命令。

### 新增
- 项目设计与开发文档全集（docs/）：规划、需求、架构与 ADR、数据库、接口、前端、开发规范、部署运维、安全设计。
- 根目录 README、.gitignore、.env.example（仅占位符）、LICENSE（MIT）、.gitattributes。
- 服务器侧 Qdrant 编排文件 deploy/server/docker-compose.yml。
- 对话上下文管理设计（docs/02-architecture/07-对话上下文管理.md）与 ADR-0006：按 token 预算组装历史、异步滚动摘要、关键事实槽、按需回查；同步更新 RAG 流程、数据库、功能需求、管理接口、管理后台、开发计划、评测指南。
- 意图识别设计（docs/02-architecture/08-意图识别.md）与 ADR-0007：规则层 + condense 合并分类 + 向量快速通道（M5+），6 类意图（kb_qa、chitchat、human、out_of_scope、action、clarify）与路由，寒暄不再计入连续拒答；同步更新 RAG 流程、数据库、功能需求、管理接口、开放接口、管理后台、开发计划、评测指南、安全设计、文档索引。
