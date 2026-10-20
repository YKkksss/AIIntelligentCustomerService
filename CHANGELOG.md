# 更新日志（CHANGELOG）

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 新增（M4.4 ③④ 调试对话页面与 AI 交互组件，2026-10-20）
- 前端按 ADR-0019 接入 ai-elements-vue（copy-in）：`components/ai-elements/{conversation,message,loader}`、`components/ui/collapsible`、`lib/utils.ts`（`cn()`）；Tailwind v4 只引 `theme.css` + `utilities.css`，不引 preflight，语义变量限定在 `.aics-ai` 作用域，避免重置 Element Plus。
- `api/debug.ts`：`parseFrame` 解析 SSE 帧、`streamTurn` 用 `fetch` + `ReadableStream` 逐帧派发（axios 浏览器端不支持流式）、`useTurnAbort` 管理停止；`views/DebugView.vue` 按设计稿 04 画板两栏（左聊天 692 / 右检索面板 464），右栏展示意图、四路命中数、`timings`、引用、tokens、压缩结果与原始 trace。
- 路由 `/apps/:appId/debug`（`chat.debug`）+ 应用菜单「调试对话」解禁；`vite.config.ts` 加 `@tailwindcss/vite`。

### 新增（M4.4 ⑤ 调试对话验收脚本，2026-10-20）
- `backend/scripts/e2e_m4/e2e_m44_debug.py`：八步 **22 项判定**（登录与演示应用、首轮 SSE 帧序与 trace、落库 `channel=debug`、临时参数只影响本轮、历史接口、viewer 脱敏、压缩写 `jobs(type=compact)`、非法 `debug_config` 422 且不新建会话），真 HTTP + 真 PG/Redis，假模型 0 元；结果 `results/e2e_m44_debug.json`。

### 测试（M4.4 ③④⑤，2026-10-20）
- 前端 Vitest 63 例 / 10 文件、覆盖率 82.97% 语句（`api/debug.spec.ts` 10 例：帧解析、流式派发、握手错误体、停止）；`vue-tsc`、eslint、prettier、`vite build` 全过（`DebugView` chunk 433.91 kB / gzip 170.83）。
- 验收脚本 22/22；本步只动前端与脚本，后端单测 1137 / 集成 277 项维持不变。
### 新增（M4.4 ② 管理版调试对话接口，2026-10-20）
- `modules/chat/debug.py`：`merge_debug_config` 把调试页的临时 `rag_config` 深合并后按应用配置同一套 pydantic 规则校验——未知字段也报 422 并带 `loc`（`AppRagConfig` 是 `extra="ignore"`，拼错的键不能静默丢弃）；`DebugChat.history` 返回会话元信息 + 最近 50 条消息（含 `citations` 与 `retrieval_trace`）；`DebugChat.compact` 先写 `jobs(type=compact)` 再调 `memory.compact_now`，成功与失败都回写 `status`/`finished_at`；`message_dict` / `masked_trace` 对没有 `app.write` 的只读成员打码手机号与订单号。
- `modules/chat/admin_router.py`：`POST /api/v1/admin/apps/{app_id}/debug/chat`（SSE，`channel=debug`、`run_mode=debug` 由路由写死）、`GET …/debug/conversations/{conversation_id}`、`POST …/debug/compact`，权限 `chat.debug`；`main.py` 挂载（OpenAPI 52 条路径）。
- `ChatService`：`TurnRequest.debug_config` + `_Prepared.rag`，临时参数只影响本轮 RAG 输入（`_rag_input` 与压缩参数都读 `p.rag`），不写回应用配置。

### 测试（M4.4 ②，2026-10-20）
- 单测 15 例（`tests/unit/test_chat_debug.py`）：深合并与默认值、字段路径、未知字段、非对象、脱敏递归、消息组包、SSE 参数透传（`channel`/`run_mode`/`created_by`/`debug_config`）、422/404/403/401、compact 透传 actor。
- 集成 6 例（`tests/integration/test_chat_debug_pg.py`，真 PG + 假模型）：debug 轮次的 `channel` 与 `messages.retrieval_trace`、调试参数不写回应用配置、非法配置 422 且不落任何行、viewer 脱敏与成员原文两种读法、compact 写 `jobs(type=compact)` 并成功回写、未知会话 404。
- 门禁：单测 **1137 passed / 1 failed（Windows 符号链接环境项）/ 278 deselected**、覆盖率 **82%**（11967 语句 / 2153 未覆盖）、本机 Docker 全量集成 **277 passed**（460.09 s）、`ruff` / `lint-imports`（13）/ `prompts`（8）全绿、真实模型 0 元。

### 调整（默认超管账号，2026-10-20）
- `aics-cli bootstrap-admin` 无参数即可用：默认 `admin@aisc.com` / `admin123456@`（`core/config.py` 的 `DEFAULT_ADMIN_EMAIL` / `DEFAULT_ADMIN_PASSWORD`，可用 `AICS_BOOTSTRAP_ADMIN_EMAIL` / `AICS_BOOTSTRAP_ADMIN_PASSWORD` 覆盖）。
- 生产守卫：`AICS_ENV=prod` 且凭据仍是默认值时 CLI 退出码 2，要求显式传强口令；新增 `tests/unit/test_cli_bootstrap_admin.py`（3 例）。
- 文档：接手指南新增 §0.1「默认超管账号」、开发环境搭建与配置表同步；待解决问题清单 ISSUE-81（已接受，生产由 `AICS_ENV` 守卫）。
### 调整（前端按设计稿还原，2026-10-20）
- 管理后台壳层按 `docs/05-frontend/ui/aics-admin.pen` 重写：220px 深色侧栏（`#001529`、`#1677FF` Logo 块、全局四项 + 应用内六项菜单）、56px 顶栏（应用切换器、面包屑、文档、铃铛、头像菜单）。
- 登录页还原设计稿 01 画板：620px 品牌面（标题、副文案、三个特性胶囊）+ 400px 白卡（账号密码、记住登录占位、忘记密码提示、页脚、依赖状态）。
- 应用列表还原 02 画板：搜索、状态筛选胶囊（含计数）、380×196 卡片（图标 / 状态 / 三段统计 / 更新时间 / 调试·进入·更多）。
- 应用配置还原 05 画板：`config_version` 标签、「版本历史（占位）/ 重置 / 保存并发布 vN」、两列分组卡片、字段中文化。
- 知识库页还原 03 画板：拖拽上传卡 + 链接导入卡 + 表格卡（文档 / 待复核切块 / 任务三个 Tab）。
- 超级管理员在 `/me` 里没有应用成员关系时，用 `/admin/apps` 补齐切换器与侧栏的应用列表；进入应用页时按路由参数切换当前应用。
- 新增 `frontend/src/styles/theme.css`（14 个设计 token + Element Plus 变量覆盖）；页面截图存档 `docs/05-frontend/ui/preview/`。
- `docs/05-frontend/01-管理后台设计.md` 写明「`.pen` 为视觉唯一来源」与实现状态对照。

### 新增（M4.3 ⑤ 里程碑验收脚本，2026-10-20）
- 新增 `backend/scripts/e2e_m4/e2e_m43_knowledge.py`：真实 HTTP + 真 PG / Redis / Qdrant + 本地文件存储，嵌入器用进程内确定性假实现（**不调模型**），九步 **35 项判定**——`bootstrap-admin` 与登录、上传（multipart + `chunk_config`）与非法配置 422、进程内跑入库作业（`done`、切块数 > 0、文档级配置生效）、列表 / 详情 / 切块 / 任务、上传前预览（不落库、`chunk_size` 变小块数变多）、切块禁用与复核（`approved` 恢复启用、写回 `flagged` 与空补丁 422）、待复核队列进出、删除后 `deleting` 与跑清理作业（详情 404、文档与切块行清零）、审计含 `kb.document.create` / `kb.document.delete` / `kb.chunk.update`、跨应用 404。结果写入 `backend/scripts/e2e_m4/results/e2e_m43_knowledge.json`。

### 修复（M4.3 ⑤，2026-10-20）
- 知识库 404 的 `details` 补对象标识：`DocumentNotFoundError` 带 `{"document_id": …}`、`ChunkNotFoundError` 带 `{"chunk_id": …}`（`backend/src/aics/modules/knowledge/service.py`）。此前是空对象，接入方只能靠路径参数回推是哪一个；与反馈接口 `NOT_FOUND` 带 `message_id`（M3.6）的既有做法对齐。

### 测试（M4.3 ⑤，2026-10-20）
- 门禁：单测 **1119 passed / 1 failed（Windows 符号链接环境项） / 272 deselected**，覆盖率 **82.06%**（11802 语句 / 2117 未覆盖）；本机 Docker 全量集成 **271 passed / 1121 deselected in 467.76s**；`ruff check` / `ruff format` / `lint-imports`（13 契约）/ `prompts check`（8 个）全绿；真实模型 0 元。
### 新增（M4.3 ④ 管理后台知识库页面，2026-10-20）
- 新页面 `/apps/:appId/knowledge`（`frontend/src/views/KnowledgeView.vue`）：Tab「文档」= 单文件拖拽上传（标题、切块预设或自定义 JSON、**上传前切块预览**）+ 状态 / 标题筛选 + 分页表格（状态标签、切块数、大小、更新时间）与行操作「切块 / 重建 / 替换 / 删除」；Tab「待复核切块」=`GET /chunks?review_status=flagged` 列表（文档、序号、命中短语、正文摘要）与「复核通过 / 禁用」；Tab「任务」= 入库 / 重建 / 删除任务与错误；写操作只对 `kb.write` 显示。
- 新页面 `/apps/:appId/knowledge/:documentId`（`KnowledgeDocumentView.vue`）：文档元信息（切块数、生效构建号、大小、来源、错误）、分页切块表（展开看正文、`enabled` 开关、复核状态、复核通过）、重建入口。
- 新增 `frontend/src/api/knowledge.ts`：类型 + `knowledgeApi` 的 14 个方法，一一对应管理端知识库路由（列表 / 详情 / 单文档切块 / 跨文档切块 / 任务 / 上传 / URL 导入 / 替换 / 重建 / 改名 / 删除 / 两条预览 / 切块启停）；multipart 只在有值时追加 `title`、`chunk_config`（避免空串被后端当值）。
- 新增 `frontend/src/utils/knowledge.ts`：切块配置文本框解析与回填（`parseChunkConfig` / `formatChunkConfig`）、文档与复核状态标签、体积与正文摘要格式化；纯逻辑放 utils 便于 Vitest 覆盖。
- 路由新增 `app-knowledge` / `app-knowledge-document`（`meta.permission = "kb.read"`，不进左侧菜单）；应用卡片与配置页工具栏各加一个「知识库」入口，应用切换不丢上下文。

### 测试（M4.3 ④，2026-10-20）
- 新增 `frontend/src/api/knowledge.spec.ts`（6 例：读取路径与查询串、multipart 只带非空字段、URL 导入的查询串、替换 / 重建 / 改名 / 删除 / 切块启停、两条预览）。
- 新增 `frontend/src/utils/knowledge.spec.ts`（8 例：空文本跟随应用配置、合法 JSON、非法 JSON 与数组、回填缩进、文档与复核状态标签、体积、摘要折叠与截断）。
- 前端 `vitest` **53 例 / 9 文件**全绿，覆盖率 **81.94% 语句 / 81.88% 行**（`src/utils/knowledge.ts` 与 `src/api/knowledge.ts` 语句 100%）；`vue-tsc -b`、`npm run lint`（eslint + prettier）、`npm run build` 全过。

### 新增（M4.3 ③ 切块复核与启停，2026-10-20）
- 新增 `GET /api/v1/admin/apps/{app_id}/chunks`：跨文档切块查询（`document_id` / `review_status=none|flagged|approved` / `enabled` / 分页），只返回各文档**生效构建号**且未删除文档的切块，条目带文档标题、`flag_reason` 与复核时间——这就是「待复核切块」列表的数据源。
- 新增 `PATCH /api/v1/admin/apps/{app_id}/chunks/{chunk_id}`：`{"enabled": false}` 禁用切块、`{"review_status": "approved"}` 复核通过（同时恢复 `enabled`，让它重新进入可检索集合）；先提交数据库再同步 Qdrant payload，向量库失败只记 `knowledge.chunk_payload_sync_failed`（重试接口幂等）；写审计 `kb.chunk.update`（`target_type="chunk"`，`diff` 只含变化字段），`reviewed_by` / `reviewed_at` 落库。
- `KnowledgeService` 增 `list_app_chunks` / `update_chunk` / `_sync_chunk_payload`；`ChunkRepository.page_for_app` 追加跨文档分页（join `documents` 过滤旧构建号与已删除文档）；`_audit` 支持 `target_type`。
- 切块不属于当前生效构建号或不属于该应用 → 404 `CHUNK_NOT_FOUND`；无可变字段/`review_status` 非 `approved` → 422；无 `kb.write` → 403。

### 测试（M4.3 ③，2026-10-20）
- 新增 `tests/unit/test_knowledge_chunk_review.py`（7 例：禁用同步 payload 与审计、复核通过恢复 `enabled` 并记复核人、无变化不提交、向量库失败不影响应答、非法复核值、列表组包与分页参数、删除中应用 404）。
- `tests/unit/test_knowledge_admin_api.py` 增 7 例（参数透传、非法 `review_status` 422、PATCH 透传 `created_by`、复核返回 `reviewed_at`、空体 422、`flagged` 入参 422、`kb.write` 403）→ 共 21 例。
- `tests/integration/test_knowledge_admin_pg.py` 增 4 例（待复核列表 + 复核通过后从列表消失且审计 `target_type=chunk`、禁用后从可检索集合移除、旧构建号切块 404、未知切块 404 且不写审计）→ 共 13 例。
- 门禁：单测 **1119 passed / 1 failed / 272 deselected**，覆盖率 **82.06%**（11802 语句 / 2117 未覆盖）；本机 Docker 全量集成 **271 passed / 1121 deselected in 499.37s**；`ruff check` / `ruff format` / `lint-imports`（13）/ `prompts check`（8）全绿。

### 新增（M4.3 ② 管理版知识库接口，2026-10-20）
- 新增 `backend/src/aics/modules/knowledge/admin_router.py`：`/api/v1/admin/apps/{app_id}` 下 **13 条**路由——九条文档路由（列表 / 详情 / 切块 / 上传 / URL 导入 / 替换 / 重建 / 改名与元数据 / 删除）、两条切块预览（新上传 / 已有文档）、`GET /jobs`、`POST /hit-test`；鉴权 `require_perm("kb.read")` / `("kb.write")`，上传 / 导入 / 替换 / 重建都接受 `chunk_config` 与 `reset_chunk_config`（后台改切块配置的入口）。
- `KnowledgeService` 增 `audit` 端口与 `preview_upload` / `preview_document` / `list_jobs`；`_audit()` 在 `kb.document.create/replace/reindex/update/delete` 与业务写在**同一条事务**里落 `audit_logs`（开放接口不传操作人，因此不产生审计行）；`update_document` 增 `created_by`。
- `KnowledgeRuntime` 增 `reranker` / `audit` 字段，`main.build_knowledge_runtime` 注入 `get_reranker()` 与 `PgAuditSink()` 并挂载 `knowledge_admin_router`；`hit-test` 未注入重排器时回落到 `get_reranker()`。

### 测试（M4.3 ②，2026-10-20）
- 新增 `tests/unit/test_knowledge_admin_api.py`（14 例：参数透传、`created_by`、`chunk_config` 解析与 422 路径、预览、`jobs` 时间序列化、`hit-test` 校验与重排器透传、401/403）与 `tests/unit/test_knowledge_audit.py`（3 例：无操作人 / 无端口不写、字段完整）。
- 新增 `tests/integration/test_knowledge_admin_pg.py`（9 例，真 PG）：审计与文档同事务、**审计失败整体回滚**、`chunk_config` 合并成完整快照、非法配置 422 精确到 `["chunk_config","chunk_size"]`、预览不写库、覆盖配置改变切块数、`jobs` 过滤、删除写审计、403/401。
- 门禁：单测 **1105 passed / 1 failed / 268 deselected**，覆盖率 **82.01%**（11722 语句 / 2109 未覆盖；唯一失败仍是 Windows 符号链接环境项 `tests/unit/test_storage.py::test_symlink_escape_rejected`）；本机 Docker 全量集成 **267 passed / 1107 deselected in 434.82s**；`ruff check` / `ruff format` / `lint-imports`（13 契约）/ `prompts check`（8 个）全绿。

### 新增（M4.2 ⑤ 里程碑验收与收尾，2026-10-20）
- 里程碑验收脚本 `backend/scripts/e2e_m4/e2e_m42_apps.py`：真 PG / Redis、不调模型，九步 **36 项判定**——建应用（`config_version=1`）、乐观并发 409 且 `details` 带 expected/current、非法字段 422 精确到 `["rag_config","fuse_top_k"]`、模型与切块预设、系统设置读写与非法值、复制不带 Key / 会话 / 文档、删除 202 + 清理后数据归零且任务行保留、重跑幂等、审计动作齐全；结果写 `results/e2e_m42_apps.json`。
- 修 `PATCH /api/v1/admin/system/settings` 的部分字段语义：与现值合并后再整对象校验（此前只给一个字段会被另外两个必填项拒成 422）。
- 修创建与复制的审计丢失：`POST /admin/apps` 与 `…/copy` 的审计写入发生在 `_create_from` 的提交之后、又没有再次提交，请求结束即回滚；现在两处补 `commit`，`app.create` / `app.copy` 才真正进审计表。
- 新增集成用例 `backend/tests/integration/test_apps_admin_pg.py` 4 例：字段级 422、旧版本 409、复制只带配置、删除后列表与详情都消失。

### 测试（M4.2 ⑤，2026-10-20）
- `e2e_m42_apps.py` **36/36**；`test_apps_admin_pg.py` 4 例；`test_system_settings_admin.py` 补「部分字段与现值合并」；`test_apps_admin_lifecycle.py` 断言创建与复制各有两次提交（业务 + 审计）。
- 后端单测 **1088 passed**、1 failed（`tests/unit/test_storage.py::test_symlink_escape_rejected`，`OSError: [WinError 1314]`，Windows 符号链接权限，环境问题）、259 deselected；覆盖率 **82.21%**（11530 语句 / 2051 未覆盖）。
- 本机 Docker 集成套件：**258 passed, 1090 deselected in 391.89s**（含 `tests/e2e` 6 例）。
- `ruff check` All passed、`ruff format` 355 文件、`lint-imports` 13 kept / 0 broken、`prompts check` 8 个 ok；前端 `vitest` 39 例。

### 新增（M4.2 ④ 系统设置与前端应用管理，2026-10-20）
- **管理接口**：`GET /api/v1/admin/system/settings`（`system.read`）与 `PATCH`（`system.write`），v1 只有 `default_session_config`（`idle_close_minutes` / `idle_notice_before_minutes` / `idle_close_notice`）；缺失时返回代码默认值，pydantic 校验失败 422 且 `details.errors[0].loc` 以 `["default_session_config", …]` 开头；写审计 `system.update`（`app_id` 为空、`target_type="system"`，`diff` 只留变化键）。
- **前端**：应用列表页（卡片：名称 / 状态 / 文档数 / 配置版本；新建、复制、删除、进入配置）、应用配置页（基础信息 + 检索与意图 + 会话 + 转人工 + Webhook 地址 + 切块（可套用预设）+ 自定义词典；`changedFields` 深比较只提交改动字段，并带 `config_version` 触发 409；422 的 `details.errors` 逐条展示；无 `app.write` 时整表只读）、系统设置页。
- 新增 `frontend/src/utils/diff.ts`（`changedFields`）、`views/{Apps,AppConfig,SystemSettings}View.vue`；`api/admin.ts` 扩 `AppCard` / `AppDetail` / `ModelEntry` / `ChunkPreset` / `SystemSettings` 与 `appApi`（list / detail / patch / create / copy / remove / models / chunkPresets）、`systemApi`；路由新增 `/apps`、`/apps/:appId`（`app.read`）与 `/settings`（`system.read`）。
- 清单登记 `ISSUE-77`（系统设置没有乐观并发，后者覆盖前者）；归档 `ISSUE-03`（删除应用接口）。

### 测试（M4.2 ④，2026-10-20）
- 后端新增 `tests/unit/test_system_settings_admin.py`（5 例：缺行回代码默认、审计只记变化键、无变化不写审计、非对象 422、字段路径前缀）与 `tests/unit/test_apps_admin_api.py` 增 3 例（读取、PATCH 透传、`system.read` / `system.write` 各自 403）。
- 前端新增 `src/utils/diff.spec.ts`（6 例）→ `npm run test:run` 共 **39 例**；`vue-tsc -b`、`eslint + prettier`、`vite build` 全过（`AppConfigView` 产物 gzip 2.7 KB）。
- 门禁：`ruff check` / `ruff format` / `lint-imports`（13 契约）/ `prompts check` 全绿；单测 **1087 passed / 1 failed / 255 deselected**，覆盖率 **82.21%**（11527 语句 / 2051 未覆盖；唯一失败仍是 Windows 符号链接环境项 `tests/unit/test_storage.py::test_symlink_escape_rejected`）；本机 Docker 集成套件 **254 passed / 1089 deselected in 460.85s**。


### 新增（M4.2 ③ 应用创建、复制与删除，2026-10-20）
- **管理接口**：`POST /api/v1/admin/apps`（201，默认配置；`name` 缺失或空白 422）、`POST /api/v1/admin/apps/{app_id}/copy`（201，只复制配置与基础信息，**不复制文档 / Key / 会话**，名称加「（副本）」）、`DELETE /api/v1/admin/apps/{app_id}`（202，`{"job_id","revoked_keys","closed_conversations"}`）。权限分别是 `app.create` / `app.copy` / `app.delete`（仅 super_admin）。
- **删除流程**：置 `status='deleting'` → 吊销该应用全部 API Key（`revoked_at`，同时删 Redis 的 Key 缓存）→ 关闭全部活跃会话（`close_reason='app_deleted'`）→ 建 `jobs(cleanup_app)` 任务 → 提交后投递 `aics.cleanup_app`；此后该应用对其它接口一律 404（`get_app` 过滤 `deleting`）。
- **清理任务** `aics.cleanup_app`（队列 `maintenance`，`modules/apps/cleanup.py`）：Qdrant 向量点 → 文件前缀 `{app_id}/` → 数据库各表 → 最后 `apps` 行；向量库或存储不可用时只记 warning、不阻断删库（孤儿点交给 reconcile）；`jobs` 行保留成平台级记录（`app_id` 置 NULL、`payload.result` 记各表删除行数），**每步幂等、可重复执行**（ISSUE-76：已 `done` 直接返回，应用不在则记 `missing`）。
- 新增 `modules/apps/cleanup.py`、`tasks/apps.py`（Celery 任务）、`ConversationRepository.close_all_active`（供其它调用方复用）；`AppsAdminService` 增加 `create` / `copy` / `delete` 与 `ActorMeta` 审计入口；`AppsAdminRuntime` 增加 `redis` 字段（删 Key 缓存）；`tasks/celery_app.py` 的 `include` 与 `task_routes` 增加本任务。
- 审计：`app.create` / `app.copy`（`diff` 带 `source_app_id`）/ `app.delete`（`diff` 记 `revoked_keys` 与关闭会话数）。

### 测试（M4.2 ③，2026-10-20）
- 新增 `tests/unit/test_apps_admin_lifecycle.py`（5 例：名称必填、创建透传字段并审计、复制加后缀且不带文档、删除置 deleting + 吊销 Key + 关闭会话 + 建任务并投递 + 审计、删除时清 Redis Key 缓存）与 `tests/integration/test_cleanup_app_pg.py`（2 例，真 PG：`DELETE` → 状态/吊销/关闭/任务行齐备 → 跑清理任务 → 各表归零、`apps` 行消失、任务行保留且 `app_id IS NULL` → 重跑返回 `already_done`；不带任务行时连跑两次为 `done` / `missing`）。
### 新增（M4.2 ② 应用读取与配置，2026-10-20）
- **管理接口**：`GET /api/v1/admin/apps`（super_admin 看全部，其余按 `user_app_roles` 过滤；`limit` / `offset` 分页，卡片带文档数）、`GET /api/v1/admin/apps/{app_id}`（完整配置 + 文档 / 切块 / 被标记切块统计）、`PATCH /api/v1/admin/apps/{app_id}`（部分更新，复用 `AppService.update_app` 的合并与校验）、`GET /api/v1/admin/models`（模型注册表）、`GET /api/v1/admin/chunk-presets`（预设展开后的完整配置）。
- **乐观并发**：`PATCH` 可带 `expected_config_version`，与库里 `apps.config_version` 不一致时返回 `409 CONFIG_VERSION_CONFLICT`（`details.expected` / `details.current`）；不带时保持旧行为，CLI 与脚本不受影响。
- **词典变更重跑**：`custom_dict` 变化时建 `retokenize_app` 任务（`app_id` + `payload={"reason": "admin.custom_dict"}`），响应回 `retokenize_job_id`，提交后投递 `aics.retokenize_app`。
- **审计**：`PATCH` 写 `app.update`，`diff` 只保留真正变化的字段（`before` / `after`），敏感键脱敏。
- 新模块 `modules/apps/admin_service.py`（`AppsAdminService` + `app_body` / `app_card` / `ActorMeta`）、`modules/apps/runtime.py`（`AppsAdminRuntime`，含 Celery 投递的 `enqueue`）、`modules/apps/admin_router.py`；`core.errors` 新增 `ConfigVersionConflictError`；`AppService.list_apps` 支持 `ids` / `include_deleting` / 分页。
- 清单登记 `ISSUE-75`（PATCH 原先没有乐观并发，后保存者会静默覆盖）与 `ISSUE-76`（`cleanup_app` 失败后应用停在 `deleting`）。

### 测试（M4.2 ②，2026-10-20）
- 新增 `tests/unit/test_apps_admin_service.py`（5 例：版本冲突、只记变化字段的审计、无变化不审计、词典变更建任务并投递、详情合并配置与统计）与 `tests/unit/test_apps_admin_api.py`（7 例：列表透传调用者与分页、详情、`expected_config_version` 剥离、非整数 422、409 透传、模型与预设、403 映射）；`tests/unit/test_error_codes.py` 因为新增 `CONFIG_VERSION_CONFLICT` 同步更新接口规范 §4。后端单测 **1070 passed**、1 failed（`tests/unit/test_storage.py::test_symlink_escape_rejected`，Windows 符号链接权限环境问题）、253 deselected；覆盖率 **82.44%**（11339 语句 / 1991 未覆盖）。本机 Docker 集成套件 **252 passed, 1072 deselected in 456.18s**；`ruff check` / `ruff format` / `lint-imports`（13 kept, 0 broken）/ `prompts check`（8 个）全绿；真实模型费用 0 元。

### 新增（M4.1 ⑤ 里程碑验收与收尾，2026-10-19）
- **验收脚本** `backend/scripts/e2e_m4/e2e_m41_admin.py`：真 PG / Redis + 真 uvicorn（不调模型），九步 **34 项**判定——`bootstrap-admin` 幂等、连续失败 5 次锁定 `423` 与 `Retry-After`、未知邮箱与错密码同文案、成员授权后权限立即生效、refresh 轮换与复用踢全端、审计动作与脱敏、一次性密码首登改密、第 11 次登录 `429`；结果写 `results/e2e_m41_admin.json`，非零退出码即失败。
- **首登改密门槛统一**：新增 `core.security.ensure_password_changed`，`require_perm` 与 `GET /admin/audit`、`/audit/actions` 共用同一口径——临时密码用户除 `GET /auth/me`、`POST /auth/password`、`POST /auth/logout` 外一律 `403`（此前 `/audit` 走 `require_user()` + 显式 `authorize` 绕过了这道门槛）。
- 管理接口参考补「`must_change_password` 的可见范围」一行。

### 测试（M4.1 ⑤，2026-10-19）
- 后端单测 **1058 passed**（新增 `test_auth_api.py::test_must_change_password_blocks_everything_but_the_password_endpoints`）、1 failed（`tests/unit/test_storage.py::test_symlink_escape_rejected`，Windows 符号链接权限环境问题）、253 deselected；覆盖率 **82.48%**（11149 语句 / 1953 未覆盖）。
- 本机 Docker 集成套件 **252 passed, 1060 deselected in 369.17s**；`ruff check` / `ruff format`（344 文件）/ `lint-imports`（13 kept, 0 broken）/ `prompts check`（8 个）全绿；真实模型费用 0 元。
- 里程碑验收脚本九步 **34/34** 通过（`backend/scripts/e2e_m4/results/e2e_m41_admin.json`）。

### 新增（M4.1 ④ 前端骨架，2026-10-19）
- **`frontend/` 骨架落地**（Vue 3 + Element Plus + Pinia + Vue Router，自建、不 fork 模板，见 ADR-0018）：`src/layouts/AdminLayout.vue` 左侧菜单从路由 `meta.menu` 派生并按 `auth.can(meta.permission)` 过滤，应用切换器（切换后当前页权限不足自动回概览）、右上用户菜单（改密 / 退出）。
- **登录与令牌**：`stores/auth.ts` 把 access/refresh 放内存 + `sessionStorage`；`api/http.ts` 的 axios 实例统一带 `Bearer`，`401 + TOKEN_EXPIRED` 用**单飞 Promise** 刷新一次后重放原请求（`_retried` 防循环），刷新失败清态回登录页；错误体统一读 `error.message` 并附 `request_id`，`suppressErrorToast` 可静默。
- **页面**：登录（`ACCOUNT_LOCKED` 文案）、概览、用户管理（搜索 / 新建 / 禁用 / 重置密码一次性弹窗 / 删除，权限 `user.admin`）、审计日志（动作筛选 + 游标「加载更多」+ 详情抽屉，权限 `audit.read`）、修改密码。
- **权限**：`utils/perm.ts` 的 `permissionsOf` / `hasPermission` 与 `v-perm` 指令（无权限直接移除节点）共用一套判断，路由守卫 `authGuard` 先补 `GET /auth/me` 再判。
- **类型从契约生成**：`scripts/gen-api.mjs`（`npm run gen:api`）让后端 dump `create_app().openapi()` 成 `openapi.json`，再用 `openapi-typescript` 生成 `src/api/schema.d.ts`（3000 行，入库；`openapi.json` 是中间文件，已 gitignore）。后端多数路由用 `JSONResponse` 没有 `response_model`，响应体仍以手写类型为准（`src/api/admin.ts`）。
- **测试基座**：`vitest.config.ts`（jsdom、`src/**/*.spec.ts`、v8 覆盖率、`lines`/`statements` 门槛 80，`.vue` 与 `main.ts` 不计入——视图渲染交给 M4.6 的里程碑级 Playwright 冒烟，见 ISSUE-74）。

### 测试（M4.1 ④，2026-10-19）
- `npm run test:run` → **33 例全绿**（`utils/perm.spec.ts` 7、`stores/auth.spec.ts` 8、`directives/perm.spec.ts` 3、`api/http.spec.ts` 5、`api/admin.spec.ts` 4、`router/index.spec.ts` 6），覆盖率 **84.88% 语句 / 86.09% 行**。
- `npm run lint`（eslint 10 + prettier）与 `npm run build`（`vue-tsc -b && vite build`）全过；`dist/assets/index-*.js` gzip 275 KB。
- 新增 dev 依赖：`vitest` / `@vue/test-utils` / `jsdom` / `@vitest/coverage-v8` / `vue-eslint-parser`（`openapi-typescript` 需 `--legacy-peer-deps`，见 ISSUE-73）。

### 新增（M4.1 ③ 审计与用户授权，2026-10-19）
- **`modules/audit`** 落地：`sink.py` 的 `PgAuditSink` 把 `core.audit.AuditEntry` 写进 `audit_logs`（**只 `session.add`、不 commit**，与业务写同一条事务）；`repository.py` / `service.py` 提供 `id` 降序游标分页（游标是 `a:<id>` 的 base64url，非法游标 → 422）；`runtime.py` 与 `admin_router.py` 暴露 `GET /api/v1/admin/audit`（`app_id` / `action` / `actor_id` / `cursor` / `page_size`）与 `GET /api/v1/admin/audit/actions`。
- **审计读权限**：平台级查询（不带 `app_id`）只有 super_admin；带 `app_id` 时按该应用的 `audit.read` 判定。路由路径没有 `app_id`，所以用 `require_user()` + 显式 `authorizer.authorize(...)`，不走 `require_perm`。
- **用户管理**：`GET/POST /api/v1/admin/users`、`PATCH/DELETE /api/v1/admin/users/{user_id}`（权限 `user.admin`）。新建用户强制首登改密；改密、禁用、改超管都会 `token_version + 1` 并删 `auth:who:` 缓存，删除是软删除 + 立即失效 token；重复邮箱 409，未知用户 404。
- **应用成员**：`GET /api/v1/admin/apps/{app_id}/members`（`app.read`）、`PUT/DELETE …/members/{user_id}`（`member.write`，角色只收 `app_admin` / `viewer`）。增删改角色后同时删 `auth:who:{user}` 与 `auth:perm:{user}:{app}` 两个缓存——ISSUE-72 的「撤销最长延迟 60 秒」因此关闭，权限变更立即生效。
- **审计动作**：`user.create` / `user.update` / `user.delete` / `member.set` / `member.remove`，`diff` 用 `core.audit.diff()` 只留变化键并脱敏（密码、密钥、令牌一律 `***`）；`app_id` 在成员动作上必填，平台级用户动作留空。
- **依赖合同**：`audit` 只依赖 `core` 与 `db.models`（不 import `auth`），写入端由 `main` 注入 `AuthRuntime(audit=...)`。

### 测试（M4.1 ③，2026-10-19）
- 新增 `backend/tests/unit/test_audit_module.py`（11 例：游标往返与四类非法游标、分页取 `page_size + 1` 判有无下一页、`page` / `actions` 协议层、403 透传）与 `backend/tests/unit/test_auth_users.py`（19 例：建用户哈希与审计、重复邮箱 409、改名不踢 token、禁用/改密/删除踢 token 与删缓存、成员增删的 404/422 与双缓存失效、`reset-password` 一次性临时密码、六个路由的状态码与 401）。
- 新增 `backend/tests/integration/test_audit_pg.py`（8 例）：审计与业务写同事务（回滚后无行）、密码在 `diff` 里被脱敏成 `***`、游标翻页两页、授权变更后 `permissions_of` 立即变化、删除用户后旧 access 401、平台级审计非超管 403、`actions` 去重、`app_id` 无外键（应用不存在也能写）。
- 门禁：单测 **1057 passed / 1 failed / 253 deselected，覆盖率 82.47%（11145 语句 / 1954 未覆盖）**（唯一失败是 Windows 符号链接特权的 `tests/unit/test_storage.py::test_symlink_escape_rejected`，`OSError: [WinError 1314]`）；本机 Docker 全量集成 **252 passed in 499.72s**；`lint-imports` 13 个合同 0 违规；真实模型费用 0 元。

### 新增（M4.1 ② 鉴权底座，2026-10-19）
- **`modules/auth`** 从空壳变成可用的鉴权模块：`passwords.py`（argon2id，`time_cost=3 / memory_cost=64 MiB`）、`tokens.py`（PyJWT HS256，access 30 分钟 / refresh 7 天，claims 为 `sub/tv/typ/jti/iat/exp`）、`repository.py`、`permissions.py`（25 个点分权限与 `super_admin/app_admin/viewer` 三角色）、`schemas.py`、`service.py`、`authorizer.py`、`runtime.py`、`admin_router.py`（`/api/v1/admin/auth/login|refresh|logout|me|password`）。
- **登录安全**：同一邮箱连续失败 5 次锁 15 分钟（423 `ACCOUNT_LOCKED` 带 `Retry-After`）、按 IP 每分钟 10 次（429）；未知邮箱、密码错误、账号禁用一律同一句「邮箱或密码不正确」，不暴露账号是否存在；成功登录按需 rehash、清除失败计数。
- **刷新轮换与复用检测**：refresh 一次性使用，Redis 里存 `{prefix}auth:refresh:{jti}`；已用过的 refresh 再次出现即视为泄露，`token_version + 1` 踢掉全部设备并记 `auth.refresh_reused`。改密同样递增 `token_version` 并返回新的一对令牌；两处都会顺手删掉 authorizer 的 `auth:who:{user_id}` 缓存（否则被撤销的 access 还能在缓存窗口内继续用 60 秒——集成用例抓到的真实缺陷）。
- **权限**：`core/security.py` 新增 `AuthedUser` 与 `Authorizer` 端口、`require_user()` 与 `require_perm(permission)` 依赖（从路径取 `app_id`，super_admin 全放行，其余查 `user_app_roles → roles.permissions`，缓存 60 秒）；`must_change_password` 的账号只放行改密。
- **审计端口**：`core/audit.py` 提供 `AuditEntry` / `AuditSink` / `redact()` / `diff()`（密码、密钥、令牌一律 `***`），登录/失败/复用/改密先写进会话、随业务事务一起提交；PG 落库留给 ③ 的 `PgAuditSink`。
- **限流下沉**：`core/ratelimit.py` 抽出 `RateLimitedError` / `Limits` / `FixedWindowLimiter`（INCR + EXPIRE 的定长窗口，Redis 故障 fail-open），`modules/apikeys` 改为复用同一异常类型。
- **迁移 `0014`**：`users` 增 `must_change_password` / `token_version` 两列，新建 `audit_logs`（`id bigint IDENTITY`、`actor_type` 三值 CHECK、`ip inet`、`diff jsonb`、两条按时间倒序的索引；不建外键，应用删除后审计仍可解释）。
- **`aics-cli bootstrap-admin`**：读 `--email/--password/--name`（缺省回落到 `AICS_BOOTSTRAP_ADMIN_*`），幂等——已存在则只提升为超级管理员，`--reset-password` 时才改密并递增 `token_version`。
- 新增依赖 `PyJWT>=2.9`（2.15.1）、`argon2-cffi>=23.1`（25.1.0）；错误码 `TOKEN_EXPIRED`（401）与 `ACCOUNT_LOCKED`（423）落地，接口规范表里不再有「未实现」的码。

### 测试（M4.1 ②，2026-10-19）
- 新增 `tests/unit/test_auth_tokens.py`（6 例：签发与解析、过期 → `TOKEN_EXPIRED`、类型混用、签名篡改、字段缺失）、`test_auth_permissions.py`（5 例：权限矩阵与迁移 0001 的种子一致）、`test_auth_service.py`（12 例：登录成功 / 未知邮箱同文案 / 禁用 / 第 5 次锁定 / 锁定期先于验密 / 刷新轮换 / 复用踢全端 / 版本过期 / 改密两种 422 / 改密成功 / me / 登出容错）、`test_auth_api.py`（7 例：登录 200、短密码与多余字段 422、423 透传 `Retry-After`、`/me` 的 401 与 200、refresh + logout、改密返回新对）。
- 新增 `tests/integration/test_auth_pg.py`（5 例，真 PG + 真 Redis）：超级管理员与 viewer 的权限差异、刷新轮换与复用检测（`token_version` 变 1、旧 access 失效）、失败落库 `failed_logins/locked_until`、改密后旧 access 401 且新哈希可验、未知用户的 token。
- 门禁：`pytest` **1026 passed / 1 failed / 245 deselected**，覆盖率 **82.15%**（10835 语句 / 1934 未覆盖；唯一失败仍是 Windows 符号链接环境用例 `tests/unit/test_storage.py::test_symlink_escape_rejected`）；`ruff check` / `ruff format` / `lint-imports`（13 kept）/ `prompts check`（8 个）全绿；真实模型费用 0 元。
- 本机 Docker 集成套件（`docker\dev-stack.cmd test`）**244 项全部通过**（408 秒），其中 `tests/integration/test_auth_pg.py` 5 例、其余为既有 239 项回归。

### 新增（M3.6 ⑤ M3 收尾，2026-10-19）
- **未知路由与 405 也走统一错误体**（ISSUE-01）：`backend/src/aics/main.py` 注册 `StarletteHTTPException` 处理器——404 → `NOT_FOUND`、405 → `METHOD_NOT_ALLOWED`（保留 `Allow` 头），其余状态码保持 Starlette 原样返回；`/v1` 前缀仍走 OpenAI 错误体。接口规范 §4 补 405 行。
- **文件超限两个入口同码**（ISSUE-32）：`backend/src/aics/modules/knowledge/service.py::_check_size` 改抛 `FileTooLargeError`（413 `FILE_TOO_LARGE` + `details.max_bytes`），与路由的分块检查一致；空文件仍是 422。
- `tests/unit/test_app_errors.py` 三条新用例（未知路径 404 信封与 `request_id`、405 信封与 `Allow` 头、`/v1` 未知路径的 OpenAI 错误体）。

### 文档（M3.6 ⑤，2026-10-19）
- [M3 测试报告](docs/06-development/10-M3测试报告.md) 新增 **§18 M3.6**（反馈接口、错误码一致性、OpenAPI 与 e2e、六条链路的真实 HTTP 端到端汇总、门禁）与 **M3 里程碑结论**；§1 的本机执行记录按「本机已有 Docker 栈」改写。
- [开发计划](docs/00-planning/03-开发计划.md)：M3 行打 ✅ 并写 **M3 里程碑总结**、M3.6 状态块；待复验清单补 4 条 M3.6 服务器项（反馈与隔离用例、接入指南 e2e、`/docs` 生产口径、里程碑真实模型 ≈0.4 元）。
- [README](README.md) 当前状态改「M3.6 完成、M3 里程碑收尾」；[代码地图](docs/00-handoff/02-实现现状与代码地图.md) 标题与里程碑行、§8 测试数字（unit 67 文件 / 648 函数 / 82.55%、集成 33 文件 / 186 函数 / 239 项）与 §9 差距行同步（404/405 与跨入口 413 标已解决）。
- [待解决问题清单](docs/00-planning/07-待解决问题清单.md)：ISSUE-01、ISSUE-08、ISSUE-32 归档，§1 保留 P1/P2 未解决项。


### 新增（M3.6 ④ OpenAPI 整理与接入指南示例，2026-10-19）
- **`core/openapi.py`**：共享的 `ErrorItem` / `ErrorEnvelope` 模型（`error.code / message / details / request_id`）与 `errors(*statuses)` 助手（12 个状态码的中文描述）；**22 条路由全部补 `summary` 与错误响应声明**——应用作用域路由统一 `401/403/404/422`（上传再加 `409/413/429`、对话加 `429`、坐席接入加 `409`），`/v1` 的两种路由用各自错误体（`{"error": {message, type, code}}`），因此只声明描述、不复用 `ErrorEnvelope`。
- `/docs`、`/redoc`、`/openapi.json` 在 `AICS_ENV=prod` 下整体关闭（`docs_url` / `redoc_url` / `openapi_url` 均为 `None`），非生产环境开放；[接口规范 §1](docs/04-api/01-接口规范.md) 的「生产环境仅管理员网段可访问」改成「生产整体关闭」（ISSUE-68 归档）。
- **测试夹具上移**：`pg_migrated` / `pg` / `redis_it` 从 `tests/integration/conftest.py` 移到根 `tests/conftest.py`，供 `tests/e2e` 复用；`tests/integration/conftest.py` 只留需要 Qdrant 与对象存储的 `ingest_migrated` / `env`。

### 测试（M3.6 ④，2026-10-19）
- 新增 `tests/unit/test_openapi_docs.py`（6 例）：每条路由都有 `summary`、非 `/v1` 的 4xx 都指向 `ErrorEnvelope`、`/v1` 只要求声明 4xx（错误体不同）、所有路由的 4xx 合集覆盖 `401/403/404/422`、生产环境三个文档地址都是 `None`、非生产分别是 `/docs` 与 `/openapi.json`。
- 新增 `tests/e2e/test_integration_guide.py`（5 例，**真 uvicorn + 真 PG/Redis + 假模型**）：把[第三方接入指南](docs/04-api/04-第三方接入指南.md)的示例逐段跑一遍——§2 `curl`（阻塞 / `-N` 流式 / `-F` 上传 + `Idempotency-Key`）、§3 `httpx` 问答与 `X-Conversation-Id` 追问、§4 OpenAI SDK（`models.list` 与应用 id、阻塞与流式）、§5 SSE 解析规则（`delta` 拼接等于 `end.answer`）、§7.4 验签示例（四头齐全且通过、篡改正文失败）。运行时预装 authenticator / chat_runtime / knowledge_runtime，不碰真模型、Qdrant 与 broker，成本 0 元。
- 本机 Docker 集成套件（docker\dev-stack.cmd test）**239 项全部通过**（551 秒），其中 tests/e2e 6 例、tests/integration 233 项；单测 **993 passed**（唯一失败仍是 Windows 符号链接环境用例）、覆盖率 **82.54%**（10162 语句 / 1774 未覆盖）。

### 测试（M3.6 ③ 错误码一致性，2026-10-19）
- 新增 `tests/unit/test_error_codes.py`（7 例）：解析[接口规范 §4](docs/04-api/01-接口规范.md) 的错误码表，与代码双向比对——① 文档里有、代码里没有的码只允许在 `PENDING` 里（`TOKEN_EXPIRED` / `ACCOUNT_LOCKED`，M4.1），实现后忘记删也会失败；② 代码里有、文档里没有的码不允许；③ 同一个码两侧的 HTTP 状态码必须一致（`PARSE_FAILED` 例外：它不是响应，只在 `documents.error` 与 job 里）；④ 遍历 `aics` 包收集 `AppError` 子类，并额外扫全仓 `code="…"` 字面量，防止新码绕过类层次悄悄出现；⑤ 每个码都要有非空的中文 `message`。
- 修正接口规范 §4 的漂移：`HANDOFF_STATE_CONFLICT` 已在 M3.4 实现，此前仍写在「随 M3/M4 落地」一档；现在只列 `TOKEN_EXPIRED` / `ACCOUNT_LOCKED` 两个登录相关码。
- 开发计划 M3.6 ② 的 `comment` 上限由 500 更正为 **2000**（与开放接口参考 §2 的契约一致，实现按契约来）。

### 新增（M3.6 ② 反馈接口与迁移 0013，2026-10-19）
- **迁移 `0013`**：`feedback` 表（`id`、`app_id`、`message_id` 唯一、`rating` ∈ {1,-1}、`comment`、`source` ∈ {api,admin} 默认 `api`、`handled` 默认 false、`eval_case_id`、`created_at` / `updated_at`；索引 `ix_feedback_app_created`）。`eval_case_id` 暂不加外键——`eval_cases` 要到 M5.1 才建表，M5.3 补约束。ORM 见 `db/models/feedback.py`（已导出）。
- **开放接口** `POST /api/v1/apps/{app_id}/messages/{message_id}/feedback`（scope `chat`）：请求 `{"rating": 1|-1, "comment": "可选"}`，响应 `200 {"id", "rating"}`。只有 `role = assistant` 的回答可反馈（否则 422 `VALIDATION_ERROR`，`details.errors[0].loc = ["path", "message_id"]`）；`message_id` 在唯一键上 `ON CONFLICT DO UPDATE` **覆盖写**，重复提交不会新增行、`id` 与 `created_at` 不变；消息不存在或不属于本应用一律 404 `NOT_FOUND`（`details.message_id`，不新增 `MESSAGE_NOT_FOUND`，见 ISSUE-67）。
- `modules/feedback`：`schemas.py`（`FeedbackBody`，`Literal[1, -1]` 与数据库 CHECK 双对齐、`comment` ≤2000 且空白归一成 NULL）、`repository.py`（`FeedbackRepository.upsert / by_message`）、`service.py`（校验消息角色 + 提交）、`runtime.py`、`router.py`；`main.build_feedback_runtime` 与 lifespan 装载（测试可预装）。
- **依赖合同改判**：`feedback` 需要读消息角色，必须依赖 `modules.chat` 的 `MessageRepository`（`chat` 不反向依赖，无环），import-linter 合同由「feedback 只可依赖 eval」改为「feedback 只可依赖 chat、eval」（见 ISSUE-69）。`tests/open_api.py` 的开放接口清单增加到 **18 条**应用作用域路由。

### 测试（M3.6 ②，2026-10-19）
- 新增 `tests/unit/test_feedback_service.py`（5 例）与 `tests/unit/test_feedback_api.py`（13 例）：写入与提交、空白评论归一、消息不存在 404、非 assistant 三种角色 422、`rating` 越界与类型错误 6 组参数化、未知字段拒绝、2001 字评论拒绝、只有 `handoff:write` 的 Key 403、响应体只含 `{id, rating}` 且透传限流头。
- 新增 `tests/integration/test_feedback_pg.py`（6 例，真 PG）：写入行、覆盖写保持 `id` / `created_at` 且 `updated_at` 变大、跨应用提交 404 且原行不变、消息不存在 404、非 assistant 422 且不落行、repository 直查。
- `tests/integration/test_isolation_pg.py` 补 `test_cross_app_message_is_404_on_feedback`（A 的 Key + A 的路径 + B 的消息 → 404），并把反馈路由纳入参数化的跨应用清单（共 34 例）。
- `tests/unit/test_apps_config_service.py` 的 scoped-repository 守卫补 `feedback` 表与 `FeedbackRepository`。
- 门禁：`ruff check` / `ruff format --check`（312 文件）/ `lint-imports`（13 契约）/ `prompts check`（8 个）全绿；单测 **980 passed**、覆盖率 **82.51%**（10143 语句 / 1774 未覆盖；唯一失败是 Windows 符号链接环境用例 `tests/unit/test_storage.py::test_symlink_escape_rejected`）；本机 Docker 集成套件全量 **233 项全部通过**（489.99 s）。真实模型费用 0 元。

### 新增（M3.5 ⑤ 演示接收端与文档收尾，2026-10-19）
- `scripts/e2e_m3/webhook_receiver.py`：可直接运行的**示例接收端**（FastAPI，`python -m scripts.e2e_m3.webhook_receiver`）。按第三方接入指南 §7.4 的示例验签（5 分钟窗口），`POST /hook` 记录每条投递（`GET /received` 可查），`POST /reset` 可换密钥、也可令它**先答 N 次 500** 用来演示退避重试。它同时是接入方的参考实现。
- `scripts/e2e_m3/e2e_m35_webhooks.py`：里程碑验收脚本（真 HTTP + 真 PG + 真 Redis，默认假模型 0 元，`--real-llm` 走真模型）。六步 26 项判定：`handoff.requested` 投递到接收端且验签通过、`accept` → `handoff.accepted`、坐席发言 → `message.created` + 用户可见 `human_agent`、`release` → `handoff.released`、接收端先答 500 时按 10 秒退避重试且重试扫描能领到该条、事件流「补发 1 帧 → 带 `Last-Event-ID` 重连只收下一帧 → 同会话触发事件时收到实时帧」。结果写 `results/e2e_m35_webhooks.json`。
- **修复（事件流与数据库连接池）**：客户端断开时 uvicorn 会取消流任务，取消若落在「读事务 / 退订」的收尾上，asyncpg 的连接会在 `close()` 中途被打断、坏连接被放回池子，**下一个请求报 `connection is closed`**（验收脚本第⑥步稳定复现）。`chat/event_stream.py` 新增 `_detached()`：把每批读、开头的会话读取与 `finally` 里的退订放进独立 task 并 `shield`，外层取消不再波及收尾；验收脚本的引擎同时补 `pool_pre_ping=True`（生产引擎本来就有）。
- 文档：M3 测试报告新增 §17 M3.5（提交表、事件与 outbox、SSE 契约、投递与退避、用例矩阵、六步验收、门禁与两条教训）；参数默认值与依据新增 M3.5 一节（22 项：保留期、`seq` 分配、渠道规则、心跳、批量、退避、扫描周期、密钥与校验点）；代码地图、数据库设计 §1.1、模块划分、开放接口参考（`/events` 标已实现）与第三方接入指南（§7 标已上线、管理接口留 M4.5）同步更新；开发计划补 M3.5 状态块与六条服务器待复验项、`README.md` 的「当前状态」更新到 M3.5；待解决问题清单按 `git log` 校正 5 份文档 55 处日期（ISSUE-58 归档）并登记 ISSUE-66。

### 测试（M3.5 ⑤，2026-10-19）
- 里程碑验收脚本 `scripts/e2e_m3/e2e_m35_webhooks.py` 本机 **26/26 通过**（`stub-llm`，3.6 秒），证据 `results/e2e_m35_webhooks.json`；`--real-llm` 与服务器节拍列入待复验清单。
- 本机 Docker 集成套件全量复跑 **225 项全部通过**（含事件流 `/events`、投递重试、跨应用 404）；单测 **962 passed**（同上 1 例环境失败）/ 226 deselected，覆盖率 **82.43%**（10030 语句 / 1762 未覆盖）。
- `ruff check` / `ruff format --check` / `lint-imports`（13 契约）/ `prompts check`（8 个提示词）全绿；真实模型费用 **0 元**。

### 测试（M3.5 ④ 用例与隔离收口，2026-10-19）
- 新增 `tests/unit/test_webhook_contract.py`（18 例）与 `tests/webhook_support.py`：把第三方接入指南 §7.4 的**验签示例代码原样搬进测试并真的跑一遍**——示例能验过我们的签名，密钥不对 / 正文被改 / 时间戳超 5 分钟 / 签名不匹配四种情况都拒绝；八种事件的 `data` 与公共字段改为**快照断言**（字段名、字段顺序、UTC 时间、中文不转义），形状一变就必须显式改这里；退避表、5 秒超时、单批 100 与文档写死的数字对齐。
- `tests/integration/test_chat_events_pg.py` 补 2 例：事件与状态变更**同事务**（同一事务里改状态 + 发事件，回滚后既没有事件行、`event_seq` 也没动）、**并发 10 条消息**的 `seq` 连续且不重复（`UPDATE … RETURNING` 把同一行串行化）。
- `tests/integration/test_webhook_delivery_pg.py` 补 2 例：本地接收端用 `httpx.MockTransport` 接真投递并按 §7.4 验签（验签通过 → `delivered`，发出去的字节与库里 `payload` 一致）、接收端返回 401 时按非 2xx 退避重试（`pending`、`attempts=1`、`last_error="HTTP 401"`）。
- 本机 Docker 集成套件全量 **225 项全部通过**；单测 **962 passed**（1 例 Windows 符号链接环境失败），覆盖率 **82.42%**（10020 语句 / 1762 未覆盖，门槛 80）。

### 新增（M3.5 ③ 出站 Webhook 投递，2026-10-19）
- `core/secretbox.py`：签名密钥工具（`whsec_` + 32 字节随机，`prefix_of` 只外泄前缀，Fernet 加解密，主密钥取 `AICS_DATA_ENCRYPTION_KEY`；44 字符的标准 Fernet key 直接当密钥，否则由口令派生）。放在 `core` 是因为 `apps` 与 `webhooks` 是**互不依赖**的独立模块（import-linter 合同），两边都要用。
- `modules/webhooks/signing.py`：`X-AICS-Event` / `X-AICS-Delivery` / `X-AICS-Timestamp` / `X-AICS-Signature: sha256=hex(HMAC_SHA256(secret, "{ts}.{body}"))`，正文与 `Content-Type: application/json`（第三方接入指南 §7）。
- `modules/webhooks/service.py`：`WebhookService.deliver`（2xx 置 `delivered`；非 2xx / 网络异常按 **10 s / 30 s / 2 min / 10 min / 30 min** 退避，共 6 次；第 6 次置 `failed`；`attempts` 每次都 +1）、`redeliver`、`due`（到期扫描并领取 60 秒）、`send_test`（`event=ping`，同步一次、不落表）、`_webhook_of`（坏配置回落空配置）；投递前重做一次 SSRF 校验（`core.netguard`，端口白名单 + `AICS_URL_FETCH_ALLOWLIST`），不跟随重定向、超时 5 秒。任务体 `run_deliver_job` / `run_retry_sweep_job` 自建 NullPool 引擎。
- `tasks/webhooks.py` + beat：`aics.deliver_webhook`（队列 `context`）与 `aics.webhook_retry_sweep`（队列 `context`，**每 10 秒**把 `pending` 且到期的投递重新入队，`FOR UPDATE SKIP LOCKED`、单批 100）——不用 Celery 长 `countdown`（RabbitMQ `consumer_timeout` 默认 30 分钟）。
- `WebhookDeliveryRepository` 新增 `load_all_apps`（按投递 ID 取一条，投递任务没有 `app_id`）与 `claim_all_apps`（领取后把 `next_attempt_at` 推后 60 秒）；`mark_delivered` 同时 `attempts += 1` 并清 `last_error`。
- `AppService.rotate_webhook_secret`（生成/轮换密钥，**明文只返回一次**，库里只留 `secret_enc` + `secret_prefix`）与保存期 SSRF 校验（`PATCH /apps/{id}` 改 `webhook_config.url` 时校验，`core.netguard`）。
- 新依赖 `cryptography>=43`（Fernet）。
- ISSUE-60（保存期 SSRF 缺失，P1）就此结案；ISSUE-59（测试事件无处落表）定案为「不落表」，两条都记进清单。

### 测试（M3.5 ③，2026-10-19）
- 新增 `tests/unit/test_webhook_delivery.py`（22 例）：密钥往返 / 标准 Fernet key / 坏主密钥、签名已知向量与四个头、投递成功 / 非 2xx 退避 / 第 6 次置 `failed` / 异常按失败重试 / 缺配置与坏密钥与地址被拒都直接置 `failed` / 非 `pending` 跳过 / `due` 领取 / `redeliver` / `send_test` 不落表、`rotate_webhook_secret` 只存密文、保存期 SSRF 拒绝与放行。
- 新增 `tests/integration/test_webhook_delivery_pg.py`（8 例）：真 PG 上事件排投递（`id == event_id`）、投递成功落 `delivered`、失败按 10 秒退避、第 6 次置 `failed`、重试扫描领取一次且不重复、`send_test` 不新增行、私网地址投递前被拒、保存私网地址被拒。
- `tests/unit/test_celery_app.py` 补 2 例：新任务的路由与 beat 节拍（10 秒、`expires` 8）、两个任务体真的调到 job。
- 单测 **944 passed**（1 例 Windows 符号链接环境失败），覆盖率 **82.42%**（10020 语句 / 1762 未覆盖，门槛 80）；本机 Docker 集成套件全量复跑 **221 项全部通过**（新增 8 项 Webhook 投递用例；既有 `test_apps_db.py::test_create_read_update_app` 随新校验调整——保存 Webhook 地址前会先做 SSRF 校验）。

### 新增（M3.5 ② 会话事件流 SSE，2026-10-19）
- `GET /api/v1/apps/{app_id}/conversations/{conversation_id}/events`（scope `chat` 或 `handoff:write`）：`text/event-stream`，`id:` 行是 `seq`、`data:` 行是与出站 Webhook **逐字段相同**的 payload，15 秒一帧 `: ping`，推完 `conversation.closed` 结束连接；`Last-Event-ID` 补发（非法值 422 `VALIDATION_ERROR`）。帧不带 `event:` 行——事件名在 payload 的 `event` 字段里，浏览器默认的 `onmessage` 也能收到。
- 新模块 `modules/chat/event_stream.py`：**先 `SUBSCRIBE` 再按 `seq > Last-Event-ID` 回表补发**，同一 `seq` 只发一次；连接期间不持事务（每批一次短事务），关闭时 `unsubscribe` / `aclose` 各自吞异常记日志（ADR-0016）。
- `modules/chat/events.py` 增加广播层：`channel()`（`{prefix}conv:{conversation_id}`）、`Publisher` 协议、`RedisPublisher`（发不出只记 `chat.event_publish_failed`，事件已落库，重连补发兜底）、`publish_pending()`（提交后把本事务累积的 `(conversation_id, seq)` 按会话去重后播一次）、`emit()` 现在把待广播的 `seq` 记到 `session.info`。
- 提交后广播接上全部写入方：`chat.service`（请求内两处提交）、`handoff.service`（accept / 坐席发言 / release）、`handoff/sweep.py`、`chat/lifecycle.py`；两个任务体（`run_handoff_sweep_job` / `run_idle_sweep_job`）自己建 Redis 客户端并在 `finally` 里关闭。`ChatRuntime` / `HandoffRuntime` 新增 `redis` / `redis_prefix` 字段与 `publisher()`。
- `chat/lifecycle.py` 原来的 `Notify` 钩子删除，统一换成 `events.Publisher`（旧钩子带事件名与 `app_id`、缺 `seq`，且 `run_idle_sweep_job` 从未传过）。

### 测试（M3.5 ②，2026-10-19）
- 新增 `tests/unit/test_chat_event_stream.py`（18 例）：`parse_last_event_id`（缺省 / 空格 / 负数 / 非整数）、`frame` 的 `id:` 与 payload 全字段、`channel` 前缀、`RedisPublisher` 的频道与吞 `RedisError`、`publish_pending` 的按会话去重与清空、`stream()` 的补发→关闭结束、先订阅后补发、空闲心跳后继续推实时事件、已关闭会话立即结束。
- 新增 `tests/integration/test_chat_events_pg.py`（6 例）：真 `/chat` 触发转人工只写 `handoff.requested`（`recent_messages` 含本轮 AI 答复）、配 Webhook 的应用逐事件排 `pending` 投递、已关闭会话走 ASGI 补发两帧且以 `conversation.closed` 结束、`Last-Event-ID: 2` 只回更新的帧、非法 `Last-Event-ID` 422、真 Redis pub/sub 实时推送（坐席 `accept` → 客户端秒收 `handoff.accepted`）。
- 三条既有用例跟着改：`test_chat_lifecycle_pg.py`（`notify=` 钩子改 `publisher=` 并改名 `test_batch_limit_and_publisher_hook`；`run_idle_sweep_job` 用 `get_settings().model_copy(update={"db_url": …})` 以便带上 Redis 配置）、`test_chat_events.py`（假 session 补 `info` 字典）、`tests/events_support.py`（记录器增加 `publish_pending` 桩，替换 `emit` / `emit_all` / `publish_pending`）；`test_isolation_pg.py` 补 `/events` 的跨应用 404。
- 本机 Docker 集成套件全量复跑 **213 项全部通过**；单测 **920 passed**（1 例 Windows 符号链接环境失败），覆盖率 **82.04%**（9773 语句 / 1755 未覆盖，门槛 80）。

### 新增（M3.5 ① 事件 outbox 与迁移 0012，2026-10-19）
- 迁移 `0012`：`conversation_events`（`type` 八值 CHECK、`UNIQUE (conversation_id, seq)`、索引 `(app_id, created_at)`）与 `webhook_deliveries`（`id` 即事件 ID、`event_id` 唯一外键、`status` ∈ pending/delivered/failed、索引 `(app_id, created_at DESC)` 与 `(status, next_attempt_at)`）；两表的 ORM 模型 `ConversationEvent` / `WebhookDelivery` 在 `db/models/event.py`。
- `modules/chat/events.py`：`emit(session, target, type, data, *, webhook=None)` 在**业务事务内**用 `UPDATE conversations SET event_seq = event_seq + 1 … RETURNING` 取 `seq`、插事件行，应用配了 Webhook 且渠道是 `api` / `openai` 时再排一条 `pending` 投递；`channel=eval` 不写事件、`debug` 写事件但不投递（ADR-0016）。`Target` / `WebhookTarget` 是两个纯数据视图，`emit_all` 按序发一组事件（先消息、后它造成的状态变化）。提交之后的 Redis 发布与任务投递留给 ② / ③。
- `modules/webhooks/payloads.py`：第三方接入指南 §7 的固定格式（公共字段 `id` / `event` / `app_id` / `conversation_id` / `user_id` / `seq` / `occurred_at` / `data`，加八种事件的 `data` 构造器）。纯拼装，不碰 DB、加密与签名。
- 八种事件全部接上：`chat.service`（`handoff.requested`、非服务时间 `handoff.offline`、pending / accepted 期间的 `message.created`，含排队等待话术）、`handoff.service`（`handoff.accepted` / `handoff.released` 与坐席消息）、`handoff/sweep.py`（两个 `TODO(M3.5)`：排队超时 `handoff.queue_timeout`、接入后无消息 `handoff.released` + `conversation.closed`）、`chat/lifecycle.py`（`conversation.idle_notice` / `conversation.closed`）。普通 AI 问答不进事件流（`needs_message_event`：`system` 或转人工 pending / accepted 期间的消息才发）。
- 读取入口：`ConversationEventRepository`（`list_after` 补发、`last_seq`）与 `WebhookDeliveryRepository`（`list_recent`、跨应用 `due_all_apps` 的 `FOR UPDATE SKIP LOCKED` 扫描、`mark_delivered` / `mark_attempt` / `reset_for_redelivery` / `count_by_status`）——② 的事件流与 ③ 的投递任务直接用，管理接口的 HTTP 面仍留 M4.5。
- 顺带解决 **ISSUE-09**（`conversations.event_seq` 建了列从不读写）与 **ISSUE-37**（无状态会话关闭失败只记 warning）：无状态会话改为在**答复落库的同一条事务**里关闭，`_close_stateless` 退化为崩溃路径的兜底（重试一次，仍失败记 `aics_stateless_close_failed` 指标）。

### 测试（M3.5 ①，2026-10-19）
- 新增 `tests/unit/test_chat_events.py`（12 例）与 `tests/unit/test_webhook_payloads.py`（9 例）：`eval` 不写事件、`debug` 写事件但不排投递、`seq` 逐条递增、投递行的 `id == event_id` + `status=pending` + envelope 全字段、未知事件名与不存在的会话报错、`emit_all` 顺序、八种 `data` 形状与 UTC 归一、摘要渲染。新增测试支撑 `tests/events_support.py`（记录器 + `install(monkeypatch)`）。
- 三条既有单测跟着改：`test_handoff_sweep.py`（假应用补 `webhook_config`，断言事件序列 `["message.created", "handoff.queue_timeout"]` / `["message.created", "handoff.released", "conversation.closed"]`、抢输不发事件）、`test_handoff_service.py`（`_fake_apps` 返回 app 配置对象、假消息补 `created_at`）、`test_chat_turn_flow.py`（`_write_assistant` 参数化加事件列：offline → `handoff.offline`、进队列 → `handoff.requested`，且**触发转人工的那条 assistant 答复本身不进事件流**）。
- 本机 `alembic upgrade head` 到 `0012` 成功；`pytest -q --cov=aics` → **902 passed**（1 例 Windows 符号链接环境失败）、覆盖率 **81.98%**（9639 语句 / 1737 未覆盖，门槛 80）；本机 Docker 集成套件全量复跑 **205 项全部通过**（381.76 s）。

### 测试（M3.4 ⑤ 里程碑验收脚本，2026-10-19）
- 新增 `backend/scripts/e2e_m3/e2e_m34_handoff.py`：模拟坐席平台用真 HTTP + 真 PG / Redis 跑完 M3.4 验收条款，11 步 24 项判定——用户说「我要人工」→ 转人工进入排队 → 坐席 `GET /handoffs?status=pending` 看到 → `accept` → 第二个坐席再抢 `409 HANDOFF_STATE_CONFLICT` → 坐席发言 → 用户 `GET …/messages` 看到 `human_agent` → `release` → 用户再问 AI 恢复 → 排队超时由 `sweep` 释放并置待回访 → 非服务时间转留言话术。默认 `--stub-llm`（预装脚本模型，不花钱），`--real-llm` 走生产装配（本机模型 Key 是占位符，列入待复验）。首轮 20/22（两条都是断言写错：交还后 `handoff_status` 按设计保留 `released`；触发转人工的那一轮永远由 AI 回），修正后 **24/24**（1.5 s），证据在 `results/e2e_m34_handoff.json`。
- 本机 Docker 集成套件全量复跑：**205 项全部通过**（357.57 s，M3.4 全量口径）。

### 文档（M3.4 ⑤ 文档收尾，2026-10-19）
- [M3 测试报告](docs/06-development/10-M3测试报告.md) 增 §15（M3.4 转人工：提交与范围、判决表与 5 个端点、三种超时与幂等口径、用例矩阵、验收脚本 11 步 24 项判定、门禁与预算）与 §16（本机集成复验 205 项）。
- [参数默认值与依据](docs/06-development/09-参数默认值与依据.md) 增 M3.4 一节（关键词与拒答阈值、`handoff_ai_mode`、排队 / 接入后无消息两种超时、服务时间窗口语义、扫描批量与顺序、幂等口径、时区与 `tzdata`）。
- [实现现状与代码地图](docs/00-handoff/02-实现现状与代码地图.md)（M3.4 ✅ + 集成 205 项、core / chat / handoff / main / cli 五行、HTTP 表 5 条坐席路由、`idle_sweep` 先 handoff 后 lifecycle、测试计数 58 文件 / 556 函数与 30 文件 / 160 函数、覆盖率 81.94%、差距表清掉 import-linter 一条）、[模块划分](docs/02-architecture/02-模块划分.md)（handoff ✅ 与 `handoff.sweep.sweep()`）、[开放接口参考](docs/04-api/02-开放接口参考.md) §5 标注已实现、[开发计划](docs/00-planning/03-开发计划.md) M3.4 ✅ 与状态块。
- [待解决问题清单](docs/00-planning/07-待解决问题清单.md)：新增 ISSUE-57（`dev-stack.cmd test <路径1> <路径2>` 把路径1 吞成 `-Service`，静默只跑第二个路径，退出码仍为 0）与 ISSUE-58（CHANGELOG 与文档把 M3.2–M3.4 的日期普遍写成 2026-10-17，实际晚 1–2 天，M3.5 开工前按 `git log` 统一校正）；修 `docker/dev-stack.ps1` 的 `Invoke-Test`（先把 `$Service` 并回路径列表再转发给 pytest，验证：两个集成文件一起跑 = 39 passed，即 9 + 30）；同时记下两条工具约定——该命令**不加 `--`**（PS 5.1 会把它当空参数名报 `ambiguous`），`.ps1` 的 UTF-8 BOM 用编辑器重写后必须补回（丢了就按 GBK 解析、中文注释直接变语法错误）。

### 新增（M3.4 ③ 服务时间与两种超时，2026-10-19）
- 转人工超时扫描 `modules/handoff/sweep.py`：`sweep(sessions, *, now=None, batch=…)` 逐应用先读 `handoff_config`（脏配置回落默认值并计入 `skipped_apps`），再按[会话生命周期与转人工](docs/02-architecture/09-会话生命周期与转人工.md) §3.6 / §3.8 做两种超时——**排队超时**（`pending` 且 `handoff_requested_at ≤ now - handoff_queue_timeout_minutes` → `released`、`handoff_end_reason=queue_timeout`、置待回访、写 `system/handoff_queue_timeout`）与**接入后无消息**（`accepted` 且 `last_activity_at ≤ now - handoff_accepted_idle_minutes` → `released`、`accepted_idle`，会话 `closed` + `close_reason=handoff_idle`，写空内容 `system/handoff_idle_closed`）。两者都是条件单行 UPDATE 并比对读到的 `handoff_requested_at` / `last_activity_at`：影响 0 行说明坐席或用户刚动过，本轮跳过——幂等、可并发、重复跑不会误杀；时钟优先用入参 `now`，缺省取数据库时钟。
- `tasks/maintenance.py::idle_sweep`（beat 每 30 s）改为**先 handoff 后 lifecycle**（§3.10），返回 `{"handoff": …, "lifecycle": …}`（两个 job 各自 `asyncio.run` + NullPool 引擎，沿用既有写法）；CLI 增 `aics-cli sweep-handoff`。
- 非服务时间（§3.7）在 `chat.service` 落库时判定：触发转人工的那一刻用 `core.schedule.in_service_hours` 判当前是否在服务时间内，不在就**不进队列**——会话直接 `released`、`handoff_end_reason=offline`、置 `needs_callback`（`callback_reason=offline`）、清掉上一轮坐席信息，本轮回复换成 `system/handoff_offline` 的留言话术（`reply_by=system`、`answer_type=null`）；服务时间取应用的 `handoff_config.service_hours`，未配置即 7×24。
- `modules/chat/schemas.py`：空内容白名单加入 `handoff_idle_closed`（§3.8 的时间线标记本就是空内容，不是漏写）。
- 依赖：显式声明 `tzdata`（Windows 没有系统时区库，`ZoneInfo("Asia/Shanghai")` 靠它；服务器 Linux 上无副作用）。

### 测试（M3.4 ③，2026-10-19）
- 新增 `tests/unit/test_handoff_sweep.py`（10 例：超时窗口取的是应用配置、条件更新比对的正是刚读到的值、被抢先或读后被动过就什么都不写、接入空闲同时关会话并留空标记、排队超时先于接入空闲、缺应用行进 `skipped_apps`、单个应用出错不影响其它应用、缺省走数据库时钟）。
- `tests/unit/test_chat_turn_flow.py`：补服务时间外触发转人工的整轮路径（判定入参、`system/handoff_offline`、`reply_by=system`）与 `_write_assistant` 的落库值（`released` / `offline` / `needs_callback` 对比 `pending` 分支）；`tests/unit/test_chat_lifecycle.py` 补空标记白名单两例。
- 本机门禁：`ruff check` / `ruff format --check`（277 文件）/ `lint-imports`（13 契约）/ `prompts check`（8 个）全绿；`pytest -q` 875 passed（1 例 Windows 符号链接环境失败）、196 deselected，覆盖率 81.94%；本机 Docker 集成套件 `docker\dev-stack.cmd test` **195 项全部通过**（6 分 16 秒，提交 ② 时是 190 项）；真实模型费用 0 元。

### 测试（M3.4 ④ 用例与跨应用隔离收口，2026-10-19）
- 新增 `tests/integration/test_handoff_pg.py`（9 例，真实 PG）：排队超时按应用配置到点才释放（`released` / `queue_timeout`、话术、待回访、空闲计时重置）；同一轮重复扫描与很久以后都不重复处理；接入后无消息超时释放并关会话（`close_reason=handoff_idle` + 空标记）；**两条竞态分支**——扫描读到 `pending` 之后坐席抢先接入、以及坐席刚说过话——条件更新 0 行时一个字都不写；两个事务同时抢单只有一个成功；软删除的应用不再被扫描；非服务时间走真 `/chat` 接口回留言话术并置待回访；`handoff_queue_timeout_minutes` ≥ 有效 `idle_close_minutes` 在保存时被拒（M2.7 校验回归）。
- `tests/unit/test_handoff_service.py` 补齐「状态 × 动作」矩阵的缺格：`accept` 在 `none` / `released` 上冲突（报的是数据库里现在的状态、不留坐席痕迹）、坐席发言在 `pending` 上冲突（参数化补 `pending`）、会话关闭后三个写动作全部 409 且连 UPDATE 都不发。
- `tests/integration/test_isolation_pg.py` 补坐席接口的跨应用 404（4 条带会话 id 的路由：A 的 Key + A 的路径 + B 的会话 → `CONVERSATION_NOT_FOUND`），并给隔离夹具装上 `HandoffRuntime`——**这 5 条路由虽然早已登记，但夹具里没有这个运行时，用例只走到认证层就 404 了**（登记 ≠ 真的打到 service），本步才真正覆盖；遗留的守卫缺口记入 ISSUE-56。
- 门禁：`pytest -q` 881 passed（1 例 Windows 符号链接环境失败）、206 deselected，覆盖率 81.94%；本机 Docker 集成套件 **205 项全部通过**（`test_handoff_pg.py` 9 例 + 隔离套件）；`ruff check` / `ruff format --check` / `lint-imports`（13 契约）/ `prompts check`（8 个）全绿；真实模型费用 0 元。

### 文档（M3.4 ③，2026-10-19）
- [待解决问题清单](docs/00-planning/07-待解决问题清单.md)：新增 ISSUE-54（待回访的六个 `callback_*` 列从本步起被写入，但列表接口与「标记已回访」在 M4.5 才有——标记进去暂时查不出来）与 ISSUE-55（偏差：扫描落在新模块 `modules/handoff/sweep.py`，而不是计划里的 `handoff.service.sweep()`）。

### 新增（M3.4 ② 转人工状态机与坐席接口，2026-10-18）
- 坐席接口 5 个端点（`modules/handoff/router.py`，`prefix=/api/v1/apps/{app_id}`）：`GET …/conversations/{id}/handoff`（状态查询，接受 `chat` 或 `handoff:write`）、`POST …/handoff/accept`（抢单接入，`pending → accepted`）、`POST …/handoff/messages`（坐席发言，201）、`POST …/handoff/release`（交还 AI）、`GET …/handoffs`（`pending` / `accepted` 队列，`id` 升序游标分页）用 `handoff:write`。
- 状态迁移全部是**条件单行 UPDATE**（`WHERE app_id=… AND id=… AND handoff_status=…`，交还还限定会话仍 `active`）：受影响 0 行时重读数据库里的当前状态并抛 `409 HANDOFF_STATE_CONFLICT`（`details.handoff_status`），并发抢单因此只有一个成功；会话已关闭 → `409 CONVERSATION_CLOSED`（坐席接口不会自动开新会话）；不是接入的那个坐席发言或交还 → `403 FORBIDDEN`。抢单成功同时清掉上一轮遗留的坐席信息与释放时间戳。
- `chat.service.append_agent_message(...)` 是坐席消息与系统提示（`handoff_accepted` / `handoff_released`）的唯一写入口：直接调 `chat.repository` 会把「`message_count` / `last_message_at` / `last_activity_at` / 清 `idle_notice_sent_at`」的联动规则抄成第二份，空闲扫描会误判；`accepted_text` / `released_text` 为空时不写系统提示（§3.5 允许 null）。
- `core/schedule.py::in_service_hours(cfg, now)`：服务时间纯函数（区间左闭右开、不跨零点、按 `zoneinfo` 的本地钟面判定、未配置即 7×24、脏配置按不服务兜底）。放在 `core` 是为了避免 `chat` 与 `handoff` 互相依赖，入参用结构化协议 `WeeklyHours`，`rag.config.ServiceHours` 天然满足它（`core` 不许反向 import `rag`）。

### 测试（M3.4 ②，2026-10-18）
- 新增 `tests/unit/test_schedule.py`（15 例：左闭右开边界、多时段与午休、时区错位、夏令时 3 月切换、未配置 7×24、脏配置兜底）、`tests/unit/test_handoff_service.py`（18 例：抢单成功/被抢先/已关闭/不存在、坐席发言的「本人 + `accepted`」前置条件、交还的三种冲突、队列列表映射）、`tests/unit/test_handoff_api.py`（34 例：契约字段、`chat` 与 `handoff:write` 的 scope 组合、body 与 query 校验、409 / 403 / 404 映射、游标回传）。
- `tests/open_api.py` 登记 5 条坐席路由（应用作用域 11 → 16），清单守卫继续在默认套件里拦截「挂了路由没登记」。
- 本机门禁：`ruff check` / `ruff format --check` / `lint-imports`（13 契约，含新的「handoff 只可依赖 chat、webhooks、apps」）/ `prompts check`（8 个）全绿；`pytest -q` 862 passed（1 例 Windows 符号链接环境失败）、196 deselected；真实模型费用 0 元。

### 文档（M3.4 ②，2026-10-18）
- [待解决问题清单](docs/00-planning/07-待解决问题清单.md)：新增 ISSUE-53（交还备注只进日志、缺可查询审计，M4.1 接 `AuditSink`）与 ISSUE-52（偏差：`core/schedule.py` 从提交 ③ 提到提交 ②——状态查询响应里的 `in_service_hours` 不能返回恒 `true` 的假值）；更新 ISSUE-04（`HANDOFF_STATE_CONFLICT` 已落地，剩 `TOKEN_EXPIRED` / `ACCOUNT_LOCKED` 留给 M4.1）。

### 工程（M3.4 ① 依赖合同修订，2026-10-18）
- import-linter 依赖合同按 [ADR-0017](docs/02-architecture/adr/0017-模块依赖合同修订与横切能力注入.md) 修订：`handoff 只可依赖 chat、webhooks` → `handoff 只可依赖 chat、webhooks、apps`（转人工要读应用的 `handoff_config`），`chat` / `handoff` / `eval` / `feedback` 四条「X 只可依赖…」合同加 `allow_indirect_imports = true`（只约束直接 import），消除「经允许的直接依赖叠加出的间接链路被误判违规」的老问题（待解决问题清单 ISSUE-28）；分层合同与 `rag` / `memory` / `ingest` / `core` 的 forbidden 合同不变，仍检查间接依赖。
- 门禁：`lint-imports` 13 kept / 0 broken（191 files、945 dependencies）。

### 开发环境（本机 Docker 依赖服务与集成复验，2026-10-18）
- 本机 Docker 起一套与服务器同版本的依赖服务：`docker/compose.yaml`（PostgreSQL 16.15 / Redis 8.2 含与服务器一致的 ACL / RabbitMQ 4.3 / Qdrant v1.18.2，端口只绑 `127.0.0.1`，数据在 `docker/volumes/`），`docker/dev-stack.ps1`（`dev-stack.cmd` 封装）提供 init / up / down / status / check / logs / reset / test；`docker\dev-stack.cmd test` 注入 `AICS_TEST_*` 后在本机跑 `pytest -m integration`，**集成套件 190 项全部通过**（此前开发机只能整批反选）。`deploy/dev/` 下只起 Qdrant 的独立 compose 归并进 `docker/`（ADR-0002 与部署文档同步）。
- 首次真实跑通立刻查出并修复 5 个缺陷：`alembic.ini` 的中文注释让 Alembic 按 locale 解码失败（中文 Windows 下每条集成用例的 `alembic upgrade head` 都报 `UnicodeDecodeError`）；`keyusage.merge_buckets` 只认 `str` 而真实 Redis 返回 `bytes`，`last_used_at` 在生产环境永远不会更新；路由在会话关闭后序列化 ORM 对象触发 `DetachedInstanceError`（新增 `KnowledgeService._fresh()` 统一在会话内刷新）；**M3.3 的无状态判定回归**——「`conversation_id is None` 即无状态」把原生 `/chat` 首轮也当成无状态（关会话 + 跳过记忆写入），导致原生接口多轮记忆整体失效，改为显式 `TurnRequest.stateless`、仅 `/v1` 缺 `X-AICS-Conversation-Id` 时置位；集成套件自身五处问题（Redis ACL 前缀、teardown 顺序、`source_type`、两条无校验力的断言、`reindex` 的作业名）。
- 门禁：集成 190 passed（本机 Docker 栈）；单元 795 passed / 1 failed（Windows 符号链接特权，环境问题）/ 191 deselected，覆盖率 81%；`ruff check` / `ruff format --check` / `lint-imports`（13 契约）/ `prompts check`（8 个）全绿；真实模型费用 0 元。
- 修 `docker/dev-stack.cmd`：其 `rem` 注释里的中文被 cmd.exe 按 OEM 代码页（GBK）解析后**当成命令执行**，`.cmd` 的任何动作都跑不起来（与 `alembic.ini` 同类问题，ISSUE-51）；该文件改纯 ASCII 并写明「必须保持 ASCII」，同时给 `docker/dev-stack.ps1` 加 UTF-8 BOM（本机没有 `pwsh`，包装脚本实际由 Windows PowerShell 5.1 执行）。

### 文档（M3.4 子步骤计划，2026-10-18）
- 新增 M3.4 子步骤计划（[开发计划](docs/00-planning/03-开发计划.md)）：① import-linter 合同改造（ADR-0017，`allow_indirect_imports`，对应待解决问题清单 ISSUE-28）；② `modules/handoff` 状态机与坐席接口（条件单行更新 + `409 HANDOFF_STATE_CONFLICT`、人工消息走 `chat.service`）；③ 服务时间与三种超时（`core/schedule.py` 纯函数、`handoff.sweep`、`idle_sweep` 先 handoff 后 lifecycle、留 `events.emit` TODO 给 M3.5）；④ 用例（状态迁移表、并发 accept、sweep 幂等、DST 时区、开放接口清单 5 条）；⑤ 文档收尾。预算 < 0.1 元。

### 新增（M3.3 OpenAI 兼容接口，2026-10-18）
- OpenAI 兼容接口 `modules/chat/openai_router.py`：`GET /v1/models`（返回 Key 所属应用）与 `POST /v1/chat/completions`（阻塞 + `stream=true`），全部走 `ChatService.start_turn(channel="openai")`，不另拼流程；响应为 `chat.completion` + `aics` 扩展字段（`message_id` / `conversation_id` / `previous_conversation_id` / `reply_by` / `handoff_status` / `answer_type` / `handoff` / `intent` / `citations`），流式每 delta 一个 `chat.completion.chunk`、末 chunk 带扩展字段后 `data: [DONE]`，心跳沿用 SSE 注释行。
- 无状态会话与请求级历史：不带 `X-AICS-Conversation-Id` 时历史只来自请求 `messages`（按应用 `history_token_budget` 裁剪，超预算走请求内临时摘要，15 秒超时、失败退回纯裁剪），新建的会话本轮结束即以 `close_reason=stateless` 关闭（迁移 `0011` 重建 CHECK，`CLOSE_REASONS` 增第 5 个值），不压缩、不产生空闲提醒；带了该头则与原生接口一致，跨应用 `404 conversation_not_found`、`model` 不匹配 `404 model_not_found`。
- `TurnRequest` 增 `inline_history` 与 `rag_overrides`（`temperature` / `max_tokens` 只收窄到应用配置上限，落在 `chat.service._rag_input`，原生 `/chat` 与 `/v1` 共用一条通道）；`memory/context.py` 的无状态分支触发条件扩为「`conversation_id is None` 或 `inline_history is not None`」，`memory_port` 不再把空列表折叠成 `None`；`main.build_chat_runtime` 接上 `Compactor` 作为临时摘要器（此前未传 `compactor`，`openai_temp_summary=true` 会静默退化成纯裁剪）。
- `core.security` 新增 `Authenticator.authenticate_key` 与依赖 `require_key_scope`：`/v1` 路径里没有 `app_id`，应用身份由 Key 本身（`Principal.app_id`）与 `model` 字段共同决定。
- `/v1/*` 的 OpenAI 风格错误体 `{"error":{"message","type","code"}}`：`main.py` 的三个异常处理器按 `request.url.path` 是否以 `/v1/` 开头分流，原生接口行为不变。

### 测试（M3.3，2026-10-18）
- 新增 `tests/unit/test_openai_api.py`（消息列表解析、`system` 忽略、`n>1` / `tools` 忽略、参数收敛、chunk 序列化、错误体映射）与 `tests/integration/test_openai_sdk_pg.py`（官方 `openai` SDK 经 ASGI 直连，验证阻塞、流式、带 / 不带会话头与错误体解析）；无状态会话与内联历史的单测（裁剪、临时摘要、本轮即关、跳过记忆、`authenticate_key` 不比对 app_id）。
- `tests/open_api.py` 支持第二条前缀 `/v1/`（应用作用域 11 条 + `/v1` 2 条），清单守卫同时覆盖两条前缀；`test_isolation_pg.py` 补 `/v1` 跨应用语义（别人的会话头、别人的 `model`）。
- 本机门禁：`ruff check` / `ruff format --check` / `lint-imports`（13 契约）/ `prompts check`（8 个）全绿；`pytest -q` 794 passed（1 例 Windows 符号链接环境失败）、190 deselected，覆盖率 81.38%；真实模型费用 0 元。

### 文档（M3.3，2026-10-18）
- [M3 测试报告](docs/06-development/10-M3测试报告.md) 增 §10–§13（无状态会话与请求级历史、OpenAI 兼容路由、用例与隔离清单、预算与结论）；[参数默认值与依据](docs/06-development/09-参数默认值与依据.md) 增 M3.3 一节（`messages` / `user` 上限、`model` 身份、参数收敛、无状态历史口径、`stateless` 收尾、心跳、`aics` 字段、SDK dev 依赖）。
- [实现现状与代码地图](docs/00-handoff/02-实现现状与代码地图.md)（`/v1` 两条路由、迁移 `0011`、core 与 chat 模块行、测试计数 55 / 502 与 29 / 149、已知差距）、[接口规范](docs/04-api/01-接口规范.md)、[开放接口参考](docs/04-api/02-开放接口参考.md) §3 标注已实现、[模块划分](docs/02-architecture/02-模块划分.md)、[数据库设计](docs/03-database/01-数据库设计.md)（`close_reason` 增 `stateless`）同步。
- [待解决问题清单](docs/00-planning/07-待解决问题清单.md) 清扫：新增 ISSUE-37（无状态关闭失败只记 warning）、ISSUE-38（无状态请求每轮一行会话，M6.1 保留策略要覆盖）、ISSUE-39（Hindsight 记忆工具 401，开发工具链，非产品问题）、ISSUE-40 / 41 / 42（本步三处偏差）；更新 ISSUE-16（阻塞点是服务器凭据，端口实测可达）与 ISSUE-20（未推送提交）；[开发计划](docs/00-planning/03-开发计划.md) M3.3 打 ✅ + 状态块 + 4 项待复验。

### 文档（M3.3 子步骤计划，2026-10-18）
- 新增 M3.3 子步骤计划（[开发计划](docs/00-planning/03-开发计划.md)）：① 无状态会话与请求级历史（迁移 `0011` 放开 `close_reason=stateless`、`TurnRequest.inline_history` / `rag_overrides`、无状态裁剪与临时摘要接线、本轮即关会话、`authenticate_key` + `require_key_scope`）；② OpenAI 兼容路由（`/v1/models`、`/v1/chat/completions` 阻塞与流式、`/v1/*` 专属 OpenAI 风格错误体）；③ 用例与隔离清单（开放接口清单支持第二条前缀、官方 `openai` SDK 集成用例）；④ 文档收尾。预算 0 元（真实模型演示改为服务器复验项）。

### 新增（M3.2 知识库开放接口，2026-10-18）
- 知识库开放接口 9 个端点（`modules/knowledge/router.py`，`prefix=/api/v1/apps/{app_id}`）：只读 `GET /documents`（`status` / `q` / `page` / `page_size`）、`GET /documents/{id}`、`GET /documents/{id}/chunks`（仅生效版本）用 `kb:read`；写 `POST /documents`、`POST /documents/url`、`PUT /documents/{id}/file`、`POST /documents/{id}/reindex`、`PATCH /documents/{id}`（标题 / 元数据立即生效）、`DELETE /documents/{id}`（204）用 `kb:write`。上传按 1 MB 分块流式累加、超 `AICS_UPLOAD_MAX_MB` 立即 `413 FILE_TOO_LARGE`（不整体读入内存），扩展名快速拒绝，魔数仍由 `ingest.parsers` 判定。
- 幂等键 `core/idempotency.py`：`Idempotency-Key` 依赖，`SET {prefix}idem:{key_id}:{key} NX EX 86400` 写占位，成功后覆写为 `{state:done,status,body}` 供回放；同键异体 `409 IDEMPOTENCY_CONFLICT`、处理中 `409 CONFLICT`；非 2xx 与异常删占位；Redis 故障放行并告警；键长 ≤128（超长 422）、回放体 ≤64 KB。上传与替换接入。
- `KnowledgeRuntime`（`modules/knowledge/runtime.py`）与 `main.build_knowledge_runtime`：知识库路由按 chat 同样的方式从 `app.state` 取运行时，测试可注入假实现；`TASK_QUEUES` 迁到 `modules/knowledge/service.py` 作为任务→队列的唯一来源，`cli._task_queues()` 复用。
- `KnowledgeService` 新增 `list_documents` / `get_document` / `list_chunks` / `update_document`；`ingest/repository.py` 新增分页与 ILIKE 转义（`like_pattern`、`page_for_app`、`page_for_document`）；新依赖 `python-multipart`（`pyproject.toml` + `uv.lock`）。
- 修 `KnowledgeRuntime` 的接线缺陷：`service()` 曾把文件存储当向量库传、漏传必填 `storage`，`build_knowledge_runtime` 又传了 `KnowledgeService` 不接受的 `reranker`——真实 lifespan 下任何知识库路由都会 `TypeError`（假运行时单测与本机跑不了的集成测试都照不到），已修复并补形参映射回归用例。
- 统一 `DocumentNotFoundError`：原先 `ingest/service.py`（`NOT_FOUND`）与 `modules/knowledge/service.py`（`DOCUMENT_NOT_FOUND`）各一份，现合并到 `core/errors.py` 一份（码值 `DOCUMENT_NOT_FOUND`），因为 import-linter 层次契约禁止 `ingest → modules`。

### 测试（M3.2，2026-10-18）
- 新增 `tests/unit/test_idempotency.py`（占位 / 回放 / 冲突 / 放行 / 异常清理）、`tests/integration/test_knowledge_api_pg.py`（真 PG + 假嵌入器与假抓取器：上传、去重、幂等回放与冲突、替换未变、PATCH、删除、URL 导入、413/400）；`tests/unit/test_knowledge_api.py` 扩到读写 9 个端点与运行时接线回归；`tests/integration/test_isolation_pg.py` 补文档级与写接口的跨应用 404；`tests/open_api.py` 清单扩到 11 条（清单守卫要求“挂载了才能登记”）；`tests/fake_redis.py` 的 `set` 支持 `nx`。
- 本机门禁：`ruff check` / `ruff format --check` 全绿、`lint-imports` 13 kept、提示词锁 8 个 ok、`pytest -q` 727 passed（1 例 Windows 符号链接环境失败）、覆盖率 81%。

### 文档（M3.2，2026-10-18）
- [M3 测试报告](docs/06-development/10-M3测试报告.md) 增 §6–§9（只读接口、幂等键与写接口、隔离用例扩展、预算与结论）；[参数默认值与依据](docs/06-development/09-参数默认值与依据.md) 增 M3.2 一节（分页 / `q` 上限 / 幂等 TTL 与长度上限 / 1 MB 分块 / metadata 8 KB / URL 2048 / `python-multipart`）。
- [实现现状与代码地图](docs/00-handoff/02-实现现状与代码地图.md) 更新里程碑（M3.2 ✅）、knowledge 与 core 行、HTTP 表 9 条、测试计数（53 个单测文件 / 462 个用例、81%）、已知差距（去掉重复异常类、加超限错误码跨入口不一致）；[接口规范](docs/04-api/01-接口规范.md) 错误码表把 `FILE_TOO_LARGE`、`IDEMPOTENCY_CONFLICT` 移入已实现并补幂等实现细节；[开放接口参考](docs/04-api/02-开放接口参考.md) §4 标注已实现；[模块划分](docs/02-architecture/02-模块划分.md)、[技术选型](docs/02-architecture/06-技术选型.md)（`python-multipart` 版本）同步。
- [待解决问题清单](docs/00-planning/07-待解决问题清单.md) 清扫：`kb:*` scope 已落地（归档）、`FILE_TOO_LARGE` / `IDEMPOTENCY_CONFLICT` 已实现、新增 ISSUE-32（超限错误码跨入口不一致）、ISSUE-33（幂等占位被强杀后残留 24 小时）、ISSUE-34（`%TEMP%` 下的 `struct.py` 遮蔽标准库）、ISSUE-35 / ISSUE-36（本步偏差）、归档 ISSUE-31（运行时接线缺陷）；[开发计划](docs/00-planning/03-开发计划.md) M3.2 打 ✅ + 状态块 + 2 项待复验。

### 文档（M3.2 子步骤计划，2026-10-18）
- 开发计划新增 M3.2 子步骤计划（[开放接口参考 §4](docs/04-api/02-开放接口参考.md) 的 9 个端点）：① 只读接口（列表 / 详情 / 切块列表 + `KnowledgeService` 三个方法 + 开放接口清单与隔离用例同步）；② `core/idempotency`（先于写接口落地）；③ 写接口（上传流式限额、URL 导入、替换、重建、`PATCH`、删除、`FILE_TOO_LARGE`、统一 `DOCUMENT_NOT_FOUND`、`python-multipart`）；④ 文档。含验收（接入指南 curl 原样可跑）、预算（< 0.1 元）与不在本步的范围。

### 新增（M3.1 API Key 收尾，2026-10-17）
- `last_used_at` 聚合：`modules/apikeys/keyusage.py` 在鉴权成功（限流通过）后向 `{prefix}keyuse:{yyyymmddHHMM}`（UTC 分钟桶）写一个字段并设 TTL 900 s，不写库；beat 任务 `aics.flush_key_usage`（maintenance 队列，每 60 秒，`expires` 55 秒）读最近 11 个桶（当前分钟只读不删、历史桶读后 `DEL`），按键取最大时间戳后用一条 `UPDATE api_keys SET last_used_at = greatest(last_used_at, to_timestamp(:ts))` 落库。分桶使过去的桶不可变，无需 Lua、无 `HGETALL`/`HDEL` 竞态；Redis 故障只记 warning、不影响鉴权。
- Key 的 IP 白名单：客户端 IP 取 `request.client.host`，只有它本身落在新配置 `AICS_TRUSTED_PROXIES`（逗号分隔 CIDR，默认空）内时才采信 `X-Forwarded-For` 的最右侧非可信地址（链中出现无法解析的地址按未知处理）；白名单外或地址未知 → `403 IP_NOT_ALLOWED`（`details.client_ip`），判定在 scope 与限流**之前**，被拒请求不消耗配额。解析与判定复用 `core.netguard`（新增 `resolve_client_ip`、`Allowlist.ip_allowed`），`core.security.Principal` 增加 `client_ip`，缓存记录带 `ip_allowlist`（命中 60 秒缓存照样生效），`set_ip_allowlist` 写后立即删缓存；`AICS_TRUSTED_PROXIES` 非法值启动即失败。CLI：`apikey create --ip`（可重复）与 `apikey allowip`。
- 开放接口清单 `backend/tests/open_api.py` 与跨应用隔离回归集 `tests/integration/test_isolation_pg.py`：A 应用 Key 访问 B 应用的接口 / 会话一律 404；`tests/unit/test_open_api_coverage.py` 在新增 `/api/v1/apps/{app_id}/...` 路由而没登记用例时失败（设计讨论 0009 D22）。
- `tests/fake_redis.py` 支持 `hset` / `hgetall` / `hdel`；`AICS_TRUSTED_PROXIES` 增加启动校验（非 CIDR 直接启动失败，避免静默地不信任任何代理）。

### 文档（M3.1，2026-10-17）
- 新增 `docs/06-development/10-M3测试报告.md`（§1–§5：执行记录、① `last_used_at`、② IP 白名单、③ 隔离回归集、预算与结论）。
- 参数默认值与依据补 M3.1 一节（桶粒度 / 回看窗口 / TTL / beat 周期 / `AICS_TRUSTED_PROXIES` / 判定位置）；实现现状与代码地图、配置与环境变量、接口规范（`IP_NOT_ALLOWED` 已实现）、模块划分、安全设计、接手开发指南（进度与下一步改为 M3.2）、开发计划（M3.1 打 ✅ + 状态块 + 3 项待复验）。

### 文档（待解决问题清单，2026-10-17）
- 新增 `docs/00-planning/07-待解决问题清单.md`：集中登记「当前存在但暂不解决的问题」（代码缺口、文档与实现不一致、工程与运维、设计待定、本步偏差）与「等后续模块完整后再测的问题」（服务器复验归类索引 + 按里程碑的可测项），每行含 ID、证据（文件:行）、影响、优先级与状态 / 何时处理；约定每个子步骤收尾时清扫本表，解决后移到 §7 归档并注明日期与提交。
- [文档索引](docs/README.md) 顶部新增说明并在 00-planning 表内登记该清单（这类问题的唯一来源）；[接手开发指南](docs/00-handoff/01-接手开发指南.md) §2 文档组织、§4 第 5 步（收尾时扫清单）、§9 已知差距（本表以清单为准）同步；[开发计划](docs/00-planning/03-开发计划.md) 待复验清单注明「其它类型问题不记在这里」。
- 顺手修掉清单里登记的两处文档不一致：根 `README.md`「当前状态」由 M0 进行中更新为「M3.1 已完成」并指向实现现状、待解决问题清单与开发计划；文档索引中「02-实现现状与代码地图」一行不再写死里程碑。
- [接手开发指南](docs/00-handoff/01-接手开发指南.md) §5 常设规则新增第 10 条：开发中发现但当前不解决 / 不确定 / 要等后续模块才能测的问题，一律登记进待解决问题清单。

### 文档（交接与 M3–v1.0 规划，2026-10-17）
- 新增交接文档：`docs/00-handoff/01-接手开发指南.md`（阅读顺序、约定、工作流与覆盖率门槛、常设规则、环境、模型与成本、待复验清单、已知差距、从 M3.1 开始的第一步）与 `02-实现现状与代码地图.md`。
- 新增 `docs/00-planning/06-后续里程碑详细计划.md`：M3.1–M6.5 每个子步骤的模块设计、数据 / 接口变更、实现要点与坑、测试、验收与真实模型预算；迁移编号预分配 0011–0020。
- 新增设计讨论 0009（文档审计 A1–A16、规划决策 D1–D22）、ADR-0016（会话事件 outbox 与事件流补发）、ADR-0017（模块依赖合同修订与横切能力注入）。
- 按代码更正文档：项目目录结构、模块划分（状态列、handoff → apps）、技术选型（SSE 为手写实现）、数据库设计（`rag_config` 实际结构、`conversation_events`）、错误码（`DOCUMENT_NOT_FOUND` 等）、配置项、测试策略（全包 80% 门槛）、开发环境、开发计划（默认分支、tag 现状、M5.6 / M5.7）；未实现的部分统一加"实现状态"说明。

### 新增（M2.7 会话生命周期，2026-10-17）
- `chat.lifecycle.sweep()`：按应用生效 `session_config` 发空闲提醒（`system/idle_notice`）并关闭空闲会话（`close_reason=idle`，`system/idle_closed`）。条件单行更新，重复 / 并发执行幂等；排除 eval 与暂停状态（accepted、wait 模式的 pending）；不补发过期提醒；配置无效回落平台默认；跳过 deleting 应用；有批量上限与事件钩子（设计讨论 0008）。
- Celery 任务 `aics.idle_sweep`（maintenance 队列），beat 每 30 秒（`expires` 25 秒）；`aics-cli sweep-idle`。
- M2 里程碑验收脚本 `scripts/e2e_m2/e2e_m27_milestone.py`、真实 worker 计时脚本 `scripts/e2e_m2/e2e_m27_worker.py`。

### 修复（M2.7）
- `chat.service` 准备阶段对会话行加锁，消除"刚读到 active 就被扫描关闭，写 user 消息时 409"的竞态。

### 新增（M2.6 对话编排与接口，2026-10-16）
- 开放接口鉴权（M3.1 核心提前）：迁移 `0009` `api_keys`；`modules.apikeys`（`aics_` 密钥只显示一次、HMAC-SHA256 + pepper、过期 / 吊销 / scope、Redis 缓存 60 s、分钟窗口限流 + 每日配额、`X-RateLimit-*` / `Retry-After`）；`core.security`（`Principal`、`require_scope`）；`aics-cli apikey create/revoke`。
- `chat.service`：`start_turn` / `handle_turn`，生成前落 user 消息、按转人工状态分派、组装 `RagInput` / `RagPorts`，每轮在独立任务中运行（断线仍会落库，`interrupted`）、error 落库、`refusal_streak` 与 handoff → pending，每轮调用 `capture_slots` / `maybe_compact`。
- `POST /api/v1/apps/{app_id}/chat`（阻塞 + SSE，15 s 心跳，`end` 带 `answer`）与 `GET /api/v1/apps/{app_id}/conversations/{id}/messages`（游标分页）。
- 新意图 `recall`：回问本次对话中提供过的信息，依据记忆作答（提示词 `condense_classify` v2、`recall` v1；迁移 `0010`）；设计讨论 0007。
- 真实 HTTP 端到端脚本 `scripts/e2e_m2/e2e_m26_http.py`；`eval_m25.py` 新增 `memory_use` 与 `--classify-timeout-ms`。

### 修复（M2.6）
- 带节点预算（如 classify 2 s）的 LLM 调用超时不再计入熔断，避免上游变慢时整个模型熔断、generate 返回 503（设计讨论 0007 D8）。

### 新增（M2.5 对话记忆，2026-10-16）
- `memory.context`：按 token 预算组装上下文——关键事实固定注入（按重要度，≤ 300 token）→ 滚动摘要 → 近期原文按轮填充 → 保底 `keep_recent_turns` 轮（超长单条截断）→ `history_gap`；拒答压成"（已拒答）"；无状态请求同算法裁剪 + 临时摘要钩子。`MemoryService.load_context` 与适配器 `modules.chat.memory_port.MemoryPortAdapter`；rag trace 新增 `context`（事实脱敏）。
- `memory.facts`：值规范化（NFKC、手机号去 +86 与分隔符）、ADD / UPDATE / NOOP / RETRACT 对账、同批冲突裁决、过期写丢弃（压缩不回滚新槽位值）、乐观锁冲突重试；`MemoryService.capture_slots` / `write_facts`。
- `memory.compactor`：滚动摘要与事实抽取（提示词 `compact_summary` / `extract_facts` v1）、Redis 锁 `aics:lock:compact:{conv_id}`、> 8000 token 分批、校验失败重试 1 次、`summary_version` 乐观写入；`maybe_compact` / `compact_now`；Celery 任务 `aics.compact_conversation`（队列 `context`）。
- `rag.identifiers`：postprocess 标识符一致性护栏（`identifier_mismatch` + `aics_identifier_mismatch_total`，不阻断）。
- `core.masking`（手机号 / 订单号脱敏）、`core.metrics`（进程内计数器，M6.3 导出）、`core.tokens.truncate_tokens`、`core.prompts.render_prompt`。
- 长对话记忆评测 `scripts/e2e_m2/eval_m25.py` + `memory_cases`（11 段 24–60 轮）；`history_token_budget` 评测后保持 3000（M1 测试报告 §13，设计讨论 0006）。

### 新增（M2.4 意图识别，2026-10-16）
- `rag.intent` 规则层：归一化、AC 自动机词表匹配、敏感词 / 转人工词 / 连续拒答 / 寒暄完全匹配与模板（内置 4 类 31 词，可被应用覆盖；greeting 用应用开场白 `AppProfile.greeting`）；`next_refusal_streak` 供 M2.6。
- guard 节点与分支：`blocked_reply`（敏感词）、规则转人工（`rule:keyword` / `rule:max_refusals`）、寒暄模板与 LLM 寒暄回复（提示词 `chitchat` v1，失败回落模板）。
- 无历史时意图分类与检索并行，非 kb_qa 取消检索。
- 超范围误判兜底 `scope_check`：`intent.out_of_scope_check`（默认 true）、`out_of_scope_override_score`（默认 0.8），强重排证据改判 kb_qa（设计讨论 0005）。
- 意图评测 `scripts/e2e_m2/eval_m24.py` + 57 条用例（M1 测试报告 §12）。

### 新增（M2.3 RAG 图（单轮），2026-10-16）
- 依赖 `langgraph`；`rag.graph.run_rag(RagInput, RagPorts)`：guard → load_context → condense_classify → route → retrieve → fuse → rerank → judge → generate / refuse / handoff → postprocess，非 kb_qa 意图走固定话术分支；流事件 trace（debug）→ citations → delta → `RagResult`；`run_rag_collect` 供阻塞接口 / 评测。
- `rag.schemas`：`RunMode`、`RagInput`（含 `AppProfile`、`HandoffConfig`、`tags`）、`RagResult`、`Citation`、`IntentInfo`、`TokenUsage`；`RagPorts` 新增 `llm` / `vector` / `reranker`。
- `rag.intent`：`IntentResult` 校验与 §5.2.2 兜底链；提示词 `condense_classify` / `condense` / `generate` / `judge`（v1）与 `prompt_version` 串。
- `rag.llm.LlmGateway`：LLM 节点级熔断（每模型一个，只计网络 / 超时 / 429 / 5xx）；`providers.llm.is_upstream_failure`。
- 模型注册表 `thinking`（`none` / `toggle`）与 `chat_model(thinking=...)`；`rag_config.llm_thinking`（默认 false）。
- `rag.citations`：`[n]` / `[1][2]` / `[1,2]` / 全角括号解析、越界删除、输出检查（规则泄露替换为兜底话术）。
- 冒烟脚本 `scripts/e2e_m2/smoke_m23.py`（结果见 M1 测试报告 §11）；设计讨论 0004。

### 修复
- 集成测试 `test_app_rag_update_checks_doc_override_window` 按 `app_id` 计数，消除偶发失败。

### 新增（M2.2b 检索设置、标签过滤、补充问题与命中测试，2026-10-16）
- `rag_config.retrieval_mode`（hybrid / vector / fulltext）、`fusion`（weighted / rrf）、`vector_weight`、`vector_score_threshold`、`default_tags`；**默认融合改为加权 `vector_weight=0.7`**（ADR-0015，评测见 M1 测试报告 §10）。
- 迁移 `0008`：`documents.tags`（GIN）与 `chunk_questions`。
- 标签过滤：Qdrant payload `tags` + PG `documents.tags` 双重校验；`KnowledgeService.set_document_tags`。
- 切块补充问题：`KnowledgeService.add_chunk_questions` / `list_*` / `delete_chunk_question` / `reattach_chunk_question`；与切块同集合（`kind=question`），命中映射回切块；重建按正文哈希迁移，失配转 `pending`。
- `KnowledgeService.hit_test`（只读命中测试）。
- 向量库：payload `kind` / `tags` 与索引、`set_enabled` 按 `chunk_id` 过滤、`set_document_tags`、`move_question`、`delete_points`；计数排除问题点。
- 集成测试支持 `AICS_TEST_QDRANT_URL=":memory:"`；评测脚本 `scripts/e2e_m2/eval_m22b.py`。
- 设计讨论 0003、ADR-0015（修订 ADR-0004 融合方式）。

### 新增（M2.2 检索链路，2026-10-16）
- `providers.llm.OpenAICompatibleLLM`（DeepSeek，LangChain `ChatOpenAI`）与 `providers.rerank`（`DashScopeReranker`、`LlamaCppReranker`、`NoopReranker`），工厂 `build_llm` / `build_reranker`。
- `rag.ports`（`ChunkSearch`、`MemoryPort`、`RagPorts` 与数据类型）；`knowledge.chunk_search.PgChunkSearch`（OR tsquery 关键词检索、可检索条件过滤取切块 / 父块）。
- `rag.retrieval`：retrieve（向量 + 关键词并行）→ fuse（RRF k=60、PG 可检索性复核、父子分组）→ rerank（失败降级 `rerank_failed`），带分步耗时与 trace。
- `core.textsearch.QUERY_STOPWORDS`：查询去疑问词 / 口语虚词（精确切分判定、自定义词典优先）。
- 评测脚本 `scripts/e2e_m2/eval_m22.py`（真实百炼、缓存、内存 Qdrant）；结果见 M1 测试报告 §9。

### 新增（M2.1 会话与消息数据模型，2026-10-16）
- 迁移 `0006`：`conversations`（状态 / 空闲计时 / 转人工 / 待回访 / 事件序号 / `summary*`）、`messages`（四种角色、`kind`、`token_count`、`search_text` + 生成列 `tsv` 与 GIN 索引、意图与回答字段、`prompt_version`、`citations`）、`system_settings`（种子 `default_session_config`）；CHECK 约束覆盖枚举、system 专属 `kind`、assistant 专属回答字段。
- 迁移 `0007`：`conversation_facts`（`category`、`importance`、`source`、`version`、`retracted_at`，key 白名单 CHECK，`UNIQUE(conversation_id, key)`）与追加写的 `conversation_fact_history`（操作形状 CHECK、BEFORE UPDATE 触发器拒绝更新）。
- `chat.repository`：`ConversationRepository`（创建 / 列表 / 幂等关闭）、`MessageRepository.append`（自动计算 `token_count` / `search_text`，锁会话行维护 `message_count`、`last_message_at`、`last_activity_at`、`idle_notice_sent_at`、标题）；输入模型 `NewConversation` / `NewMessage`。
- `apps.system_settings`：平台默认会话设置读写；`AppService.load()` 使用存储的平台默认。
- `memory`：`ConversationStore` 接口、`InMemoryConversationStore`、`PgConversationStore`（摘要乐观锁与最近 5 版历史、按轮读取、会话内关键词回查、版本化事实原语与追加写历史）；`core.errors` 新增 `ConversationNotFoundError`、`ConflictError`。

### 设计（设计讨论 0002 对话记忆方案，2026-10-16）
- 新增设计讨论 0002（参考 Mem0、Zep、Letta、LangGraph、ChatGPT 记忆的分层记忆架构）与 ADR-0014（修订 ADR-0006 第 4 条）。
- 关键事实 `enable_facts` 默认改为 `true`（`rag/config.py`），`conversation_facts` 由 M5 提前到 M2.1（表）/ M2.5（逻辑）；事实增加 `category`、`importance`、`source`、`version`、`retracted_at`，新增追加写的 `conversation_fact_history`，对账 ADD / UPDATE / NOOP / RETRACT。
- postprocess 增加订单号 / 手机号一致性护栏（M2.5，`identifier_mismatch`，只检测告警）；会话内回查（M5）排序改为 RRF + 时近度衰减。
- 评测新增保持率、更新正确率、混淆率；M2.5 计划 `history_token_budget` 3000 / 8000 / 16000 对比（参数默认值表标“待 M2.5 评测”）。
- 不采纳：跨会话长期记忆（列入范围定义 v2）、Agent 工具自管理记忆、知识图谱、衰减与冷热分层。
- 同步：对话上下文管理、RAG 流程设计、数据库设计、开发计划、功能需求 FR-CHAT-12（P1→P0）/13、术语表、模块划分、管理接口示例、评测指南。

### 测试（M1 第二轮检索评测，2026-10-16）
- `scripts/e2e_m1/build_eval_r2.py`（general 应用 + 02 法规文档带文档级 legal 预设）；`eval_retrieval.py` 支持 `--apps/--variants/--rerank/--out`，重排改为按 (问题, 文档) 缓存。
- 测试报告新增 §8 第二轮：文档级 legal 使法规题向量 Hit@1 0.56 → 0.89、整体 0.882 → 0.941；PDF 行合并指标不变；阈值 0.5 / 0.8 复测维持。参数默认值表新增文档级 chunk_config 与 PDF 行合并两行；待复验清单新增真实 PDF 样本。

### 修复
- `documents.chunk_config` 未设置时存为 JSON `null` 而非 SQL NULL，导致按 `IS NULL` 区分“跟随应用”的查询（应用 `rag_config` 窗口校验、统计）把所有文档当成有覆盖；列改为 `JSONB(none_as_null=True)`（真实 worker 构建第二轮评测应用时发现）。

### 改进（M1.7 PDF 行合并，2026-10-16）
- PDF 逐行文本块按字体字号、行距、是否排满、首行缩进、列表项合并成段（中文直接拼接、英文加空格、连字符断词拼回）；双栏页按栏阅读顺序；保留纯空格 span（修复“Storage1.8”式丢词间距）。合成语料 PDF 段落数：论文 162 → 54、合同 122 → 30。

### 新增（M1.7 文档级切块配置，2026-10-16）
- 迁移 `0005`：`documents.chunk_config`（文档级覆盖，保存合并校验后的完整快照，NULL = 跟随应用）。
- `KnowledgeService`：`upload_file` / `import_url` / `replace_file` / `rebuild_document` 可带 `chunk_config`（替换 / 重建另有 `reset_chunk_config`）；`set_document_chunk_config`；生效配置变化只重建该文档，构建成功切换时写入；`rebuild_app` 默认跳过有覆盖的文档（`skipped_override`，`include_overridden` 全量）。
- 上下文窗口规则覆盖文档级配置（`check_chunk_window`；应用 `rag_config` 修改按最坏情况校验）；`merge_chunk_config` 应用级与文档级共用。

### 变更（按 M1 评测采纳默认值，2026-10-16）
- `rag_config.rerank_threshold` 默认 0.35 → 0.5，`judge_gray_upper` 默认 0.5 → 0.8（依据 M1 端到端测试报告 §4/§5，待服务器复验）；其余检索参数经评测确认保持不变。
- 新增 [参数默认值与依据](docs/06-development/09-参数默认值与依据.md) 作为默认值唯一来源；测试策略与文档索引登记常设规则“每轮测试后记录结果并采纳最佳值为默认”。

### 测试（M1 端到端测试与检索质量评测，2026-10-16）
- `backend/scripts/e2e_m1/`：可复现的虚构公司语料（7 份 4k–26k 字主文档 + 超大表 + 15 类边界文件）、37 个端到端场景（真实 worker + 百炼向量化）、61 题检索评测（向量 / 全文 / RRF / RRF+重排，前缀与父子对比）、beat 加速验证。
- 新增 [M1 端到端测试报告](docs/06-development/08-M1端到端测试报告.md)；待复验清单更新开发机已验证项，新增 M2 拒答阈值复核。

### 修复
- 文本清洗去除 DEL 与 C1 控制字符；TXT 解码后无法解码字符占比 > 5% 判为乱码并拒绝。
- 切块缩减重叠时只保留完整句子，避免新块以半句话开头。

### 新增（M1.6 知识库服务、对账与兜底，2026-10-16；M1 里程碑本地验收通过）
- `KnowledgeService`：`upload_file`、`import_url`（导入即抓取存快照，按内容 SHA-256 去重）、`replace_file`（FR-KB-08 三条哈希规则）、`rebuild_document` / `rebuild_app`、`delete_document`（立即不可检索 → `cleanup_document`）、`request_reconcile`。
- `IngestService.run_reconcile` / `reconcile_app`（逐文档对账，区分 mismatch / orphan）、`run_sweep`（超时删除、遗留旧版本、孤儿点）；删除清理覆盖全部文件版本并更新 job 状态。
- `QdrantVectorStore.set_document_enabled`、`count_by_document`（facet 精确计数）。
- Celery：`aics.reconcile`、`aics.sweep_knowledge` 及 beat（每日 03:30 对账、每小时兜底）。
- `aics-cli reindex` / `reconcile`（`--inline` 本进程执行；否则按任务名经 broker 投递）。
- 集成测试共用 fixture 移至 `tests/integration/conftest.py` 与 `ingest_support.py`。
- 开发计划：M1.6 ✅、M1 里程碑状态（tag `v0.2.0` 待服务器复验），待复验清单新增 M1 真实 worker 验收与 URL 真实抓取。

### 新增（M1.5 分词、注入检测与去重，2026-10-16）
- `ingest/injection.py` + `injection_patterns.yaml`：入库注入检测（中英文规则、NFKC / 空白 / 零宽字符归一化）；命中切块 `review_status=flagged`、`flag_reason`，Qdrant payload `enabled=false`；父子模式父块命中则其子块全部待复核。
- `IngestService.run_retokenize` 与任务 `aics.retokenize_app`（队列 ingest）：`custom_dict` 变化后分批只重算 `search_text`，不重新向量化；被新词典取代时自动让位；构建期间词典变化在切换时补算。
- `modules/knowledge/service.py`：`upload_file`（同应用 SHA-256 去重，并发上传由唯一索引兜底，删除中返回 `409 DOCUMENT_BUSY`）、`request_retokenize`。
- 开发计划新增"待复验清单"（M1.4 真实 worker + 百炼端到端、M1.5 retokenize 真实 worker，服务器联调时补做）。

### 新增（M1.4 入库流水线，2026-10-16）
- `ingest/service.py`：版本化构建 `run_build`（清半成品 → 解析 → 预处理 → 切块 → 写父块 / 切块 → 分批向量化 → 事务切换 → 投递清理）、`run_cleanup`、切块预览 `preview` / `preview_document`（不向量化、不写库）。
- `ingest/indexer.py`：`build_embed_text`（上下文增强前缀，`chunks.text` 保持原文，向量化文本由此函数统一派生、不单独存储）；Qdrant payload 带 `parent_id`。
- `ingest/preprocess.py`：`normalize_whitespace` / `remove_urls_emails`；`ChunkConfig` 新增 `parent_mode`（paragraph / full_doc）、`full_doc_max_tokens`、`child_separators`、`contextual_prefix`。
- Celery 任务 `aics.ingest_document`（ingest）、`aics.cleanup_document`（maintenance）。
- 迁移 `0004`：`documents.indexed_title`。
- repository 移至 `aics/ingest/repository.py`（knowledge 模块重新导出）；import-linter“任务只调用 service”合同改为只禁止直接 import。

### 文档（设计讨论记录，2026-10-16）
- 新增 `docs/09-design-discussions/`（设计讨论记录目录与索引，今后所有设计讨论在此记录）；首篇 0001《知识库切块与检索设置（参考 Dify）》与 ADR-0013。
- 结论同步到文档处理流水线、RAG 流程设计 §4.2、数据库设计、Qdrant 设计、管理接口、功能需求 FR-KB-11~20；开发计划扩充 M1.4，新增 M2.2b、M4.3b、M4.6b。

### 新增（M1.3b 切块预设、手动选项与父子切块，2026-10-16）
- 切块预设 `rag/chunk_presets.yaml`（general / legal / paper / faq / manual / contract），`list_chunk_presets()`（`GET /admin/chunk-presets`）；未选预设取 general，选预设后可逐项修改，`preset` 记录来源。
- `chunk_config` 新增 `strategy`、`separators`（字面量）、`split_by_heading`、`keep_tables`、`mode`、`parent_size` / `child_size` / `child_overlap`；父子模式 80% 窗口按 `parent_size` 校验。
- chunker：五种策略（heading / article / section / faq / clause）与父子模式（`split_document` → `ChunkResult`）。
- 迁移 `0003`：`chunk_parents` 表、`chunks.parent_id`；`ChunkParentRepository`。
- 文档：文档处理流水线 §3.3、数据库设计 §3.7.1、RAG 流程设计 §4.1（父块去重）、Qdrant 设计、管理接口、功能需求 FR-APP-04、ADR-0012。

### 新增（M1.3 解析与切块，2026-10-16）
- `ingest/parsers/`：`ParsedDoc` / `Block`；PDF（PyMuPDF：字号推断标题、去页眉页脚、表格识别、跨页段落合并）、DOCX（标题样式、列表、表格、zip 炸弹防护）、Markdown、TXT（编码检测，GBK 按 GB18030）、HTML（trafilatura）、URL 导入（重定向逐跳校验、15s / 10MB 限制）；扩展名白名单 + 魔数校验。
- `core/netguard.py`：出站 URL SSRF 校验（私网 / 回环 / 链路本地 / 元数据地址、IPv6 内嵌 IPv4、整数 IP、端口限制、`AICS_URL_FETCH_ALLOWLIST`、对端 IP 复核），供 URL 导入与后续 Webhook 共用。
- `ingest/chunker.py`：标题路径、按 token（`core.tokens`）累加、句子级重叠、表格整块 / 超长按行拆分并重复表头。
- 测试：5 种格式样例文件与解析 / 切块快照；切块长度与重叠性质测试；SSRF 与 URL 抓取测试。
- 依赖：pymupdf、python-docx、trafilatura、charset-normalizer。

### 新增（M1.2 数据表与应用配置，2026-10-16）
- 迁移 `0002`：apps、user_app_roles、documents、chunks（`tsv` 生成列 + GIN）、jobs；ORM 模型与 repository（`AppRepository`、`MemberRepository`、`DocumentRepository`、`ChunkRepository`、`JobRepository`，均继承 `AppScopedRepository`）。
- `rag/config.py`：`AppRagConfig`、`IntentConfig`、`HandoffConfig`（含 `ServiceHours`）、`ChunkConfig`、`SessionConfig`/`EffectiveSessionConfig`、`WebhookConfig` 唯一定义；`validate_app_config` 做模型注册表、80% 窗口、压缩模型窗口、消息向量开关、排队超时 < 空闲关闭等跨对象校验。
- `apps.service`：创建 / 读取（`404 APP_NOT_FOUND`）/ 部分更新（`rag_config.intent` 深合并、`config_version` 与 `chunk_config_version` 维护、`custom_dict` 变化标记）；非法配置一次列出全部错误 `422 VALIDATION_ERROR`。
- 文档：数据库设计补齐 apps DDL 的 `session_config`/`webhook_config` 与各配置取值范围；模块划分说明 apps 的 `app_id` 同义属性。

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
