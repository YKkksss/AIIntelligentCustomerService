# 更新日志（CHANGELOG）

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

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
