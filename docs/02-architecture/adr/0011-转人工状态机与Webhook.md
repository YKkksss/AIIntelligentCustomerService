# ADR-0011 转人工状态机与 Webhook
- 状态：已接受（2026-10-15）；取代 ADR-0008 第 7 条中"转人工后会话保持 active、AI 照常回答"的部分
- 背景：v0.1 的转人工只是响应里的 `handoff=true`，人工坐席无法收到通知、无法把回复写回会话，AI 与人工可能同时回答。
- 决策：
  1. `conversations.handoff_status`：`none → pending → accepted → released`（released 可再次 pending），取代布尔列 `handoff`；响应字段 `handoff` 保留为"本轮是否触发转人工"。
  2. 进入 pending 时立即通过固定格式出站 Webhook（应用 `webhook_config.url`，HMAC-SHA256 签名，6 次退避重试）推送 `handoff.requested`（含原因、意图、摘要、最近消息）；接入方按此格式适配自己的坐席平台。
  3. 坐席入站接口（API Key scope `handoff:write`）：accept、发送人工回复（`role=human_agent`）、release；状态不符 `409 HANDOFF_STATE_CONFLICT`。
  4. 应用配置 `handoff_config.handoff_ai_mode`：`continue`（pending 期间 AI 继续回答）/ `wait`（pending 期间 AI 不回答，回复固定等待话术）；accepted 后两种模式 AI 都停止，release 后恢复。
  5. 空闲关闭在 accepted（任何模式）与 wait 模式的 pending 期间暂停。
  6. （2026-10-15 补充）超时兜底，均由 `idle_sweep`（30 秒）执行，不新增状态，结束原因记 `handoff_end_reason`：排队超时 `handoff_queue_timeout_minutes`（默认 5）→ released(`queue_timeout`)、发 `queue_timeout_text`、置待回访、事件 `handoff.queue_timeout`；接入后双方无消息 `handoff_accepted_idle_minutes`（默认 30）→ released(`accepted_idle`) 并关闭会话；可选服务时间 `service_hours`（周计划 + 时区，null = 7×24），非服务时间触发转人工不排队，直接发 `offline_text`、置待回访、事件 `handoff.offline`。后台强制释放保留。
  - 未选方案：新增 `timeout` 状态——超时后行为与 released 相同，只会让分派表多一个等价分支。
- 后果：新增 `aics.modules.handoff`、`aics.modules.webhooks`、表 `webhook_deliveries`、待回访列（`needs_callback` 等）、`AICS_DATA_ENCRYPTION_KEY`；接入方需实现签名校验与按事件 ID 去重。详见[会话生命周期与转人工](../09-会话生命周期与转人工.md)、[第三方接入指南 §7](../../04-api/04-第三方接入指南.md)。
