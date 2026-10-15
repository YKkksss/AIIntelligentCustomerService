# ADR-0005 接口优先，提供 OpenAI 兼容接口
- 状态：已接受；会话关联部分被 [ADR-0008](0008-记忆模块与对话职责划分.md) 细化（`X-AICS-Conversation-Id` 请求头）
- 决策：管理后台只调用公开 HTTP 接口；对外同时提供原生 `/api/v1/chat`（功能完整：引用、转人工）和 `/v1/chat/completions`（`model`=应用 ID，引用放在扩展字段 `aics`）。
- 后果：兼容层需维护字段映射；OpenAI 接口无状态，多轮由调用方传 messages，服务端不使用 conversation_id 历史（可通过 `user` 字段 + header 关联会话日志）。
