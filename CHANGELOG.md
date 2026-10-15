# 更新日志（CHANGELOG）

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

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
