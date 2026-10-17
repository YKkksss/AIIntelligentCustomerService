# ADR-0016 会话事件 outbox（conversation_events）与事件流补发
- 状态：已接受（2026-10-17，落地 M3.5）；补充 [ADR-0010](0010-会话空闲关闭与会话事件推送.md)、[ADR-0011](0011-转人工状态机与Webhook.md)
- 背景：[会话生命周期与转人工 §4](../09-会话生命周期与转人工.md) 要求同一组事件经 Webhook、SSE 事件流、轮询三条通道送达，SSE 断线重连要按 `Last-Event-ID` 补发。原设计只有 `webhook_deliveries`，它只在应用配置了 Webhook 地址、且渠道不是 debug / eval 时才写入，无法为所有会话补发；`seq` 怎么分配也没有规定。讨论见[设计讨论 0009](../../09-design-discussions/0009-文档审计与M3至v1.0规划取舍（交接）.md)。
- 决策：
  1. 新表 `conversation_events(id uuid PK, app_id, conversation_id FK ON DELETE CASCADE, seq bigint, type text, data jsonb, created_at)`，唯一 `(conversation_id, seq)`，索引 `(app_id, created_at)`。迁移 `0012`（M3.5）。
  2. 产生事件的代码（`chat.service`、`chat.lifecycle`、`handoff.service`）调用 `chat.events.emit(session, conversation, type, data)`，**与状态变更在同一事务**：`UPDATE conversations SET event_seq = event_seq + 1 … RETURNING event_seq` 取得 `seq`，插入事件行；如应用配置了 Webhook 且渠道是 `api` / `openai`，同一事务插入 `webhook_deliveries(pending)`。
  3. 事务提交后（`after_commit` 回调或 service 在 commit 之后执行）：`PUBLISH aics:conv:{conversation_id}`（只带 `seq`，订阅方回表读取完整内容），按任务名投递 `aics.deliver_webhook`（队列 `context`）。发布或投递失败只记日志：事件已落库，SSE 重连会补发，Webhook 由重试扫描兜底。
  4. SSE `GET …/conversations/{id}/events`：先订阅频道，再查 `seq > Last-Event-ID` 的事件补发，之后按通知回表读取；同一 `seq` 只发一次（订阅与补发之间的重复按 `seq` 去重）。`id:` 行即 `seq`。
  5. `channel=eval` 不产生事件；`debug` 产生事件（后台调试可见）但不投递 Webhook。
  6. 保留 7 天（`purge_expired` 清理，M6.1）；超过保留期的 `Last-Event-ID` 只能补发仍在表中的事件，客户端应再用消息列表接口对齐。
- 后果：
  - 每个事件多一次 INSERT 和一次 `event_seq` 更新。会话行在 `chat.service` 准备阶段与扫描的条件更新中本来就会加锁，不增加新的锁竞争。
  - 事件内容与 Webhook payload 完全相同（[第三方接入指南 §7](../../04-api/04-第三方接入指南.md)），由 `webhooks.payloads` 统一构造。
  - Redis 不可用时实时推送中断，但事件不丢；客户端重连后补发。
