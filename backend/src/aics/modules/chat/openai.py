"""OpenAI 兼容层的解析、序列化与错误体（开放接口参考 §3；M3.3 ②）。

只放纯函数与请求模型：`messages` → 问题 + 请求内历史 + 参数覆盖、应答与流式分片组装、
OpenAI 风格错误体映射。路由见 `openai_router.py`，`main.py` 的三个异常处理分支按路径分流。

约定（与原生接口一致，客户端无法绕过）：
- 客户端 `system` 消息忽略（应用配置的 `system_prompt` 为准）、`tools` / `functions` / `n` 等
  不支持的字段忽略（`extra="ignore"`）；
- 最后一条必须是 `user`（否则 422），它就是本轮问题；
- 历史只取最后一条 user 之前的 `user` / `assistant` 消息，由应用配置裁剪；
- `temperature` / `max_tokens` 原样进 `TurnRequest.rag_overrides`，**上限在 `chat.service._rag_input`
  收敛为 min(请求, 应用配置)**。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from aics.core.errors import ValidationAppError
from aics.db.base import new_id
from aics.modules.chat.schemas import QUESTION_MAX_CHARS
from aics.modules.chat.service import (
    ErrorEvent,
    StartEvent,
    TurnEvent,
    TurnHandle,
    TurnResult,
)
from aics.modules.chat.sse import HEARTBEAT_S
from aics.rag.ports import ChatTurn
from aics.rag.schemas import DeltaEvent

MODEL_OBJECT = "model"
COMPLETION_OBJECT = "chat.completion"
CHUNK_OBJECT = "chat.completion.chunk"
LIST_OBJECT = "list"
OWNED_BY = "aics"  # `GET /v1/models` 的 owned_by

MAX_MESSAGES = 200  # 防御性上限：历史再多也只按预算裁剪
MAX_USER_CHARS = 256  # `user` 字段只进 external_user_id（日志筛选）


class ContentPart(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str | None = None
    text: str | None = None


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    role: str
    content: str | list[ContentPart] | None = None


class ChatCompletionRequest(BaseModel):
    """`POST /v1/chat/completions` 的请求体（不支持的字段忽略，开放接口参考 §3）。"""

    model_config = ConfigDict(extra="ignore")

    model: str
    messages: list[ChatMessage] = Field(default_factory=list)
    stream: bool = False
    user: str | None = None
    temperature: float | None = None
    max_tokens: int | None = Field(default=None, ge=1)


@dataclass(frozen=True)
class Parsed:
    question: str
    history: list[ChatTurn]
    user: str | None


def _bad(field: str, msg: str) -> ValidationAppError:
    return ValidationAppError(details={"errors": [{"loc": ["body", field], "msg": msg}]})


def content_text(message: ChatMessage) -> str:
    """字符串或 `[{type:text,text}]` 数组都支持；非文本分片忽略。"""
    if isinstance(message.content, str):
        return message.content.strip()
    if message.content is None:
        return ""
    return "\n".join(
        part.text.strip() for part in message.content if part.text and part.type in (None, "text")
    ).strip()


def parse(body: ChatCompletionRequest) -> Parsed:
    """校验并拆出问题与请求内历史（不带会话头时的历史来源）。"""
    if not body.messages:
        raise _bad("messages", "messages 不能为空")
    if len(body.messages) > MAX_MESSAGES:
        raise _bad("messages", f"messages 最多 {MAX_MESSAGES} 条")
    last = body.messages[-1]
    if last.role != "user":
        raise _bad("messages", "最后一条消息必须是 user")
    question = content_text(last)
    if not question:
        raise _bad("messages", "最后一条 user 消息内容不能为空")
    if len(question) > QUESTION_MAX_CHARS:
        raise _bad("messages", f"单条消息最多 {QUESTION_MAX_CHARS} 字")
    history = [
        ChatTurn(role=m.role, content=text)
        for m in body.messages[:-1]
        if m.role in ("user", "assistant") and (text := content_text(m))
    ]
    user = (body.user or "").strip()[:MAX_USER_CHARS] or None
    return Parsed(question=question, history=history, user=user)


def overrides(body: ChatCompletionRequest) -> dict[str, Any]:
    """请求级参数 → `TurnRequest.rag_overrides`（上限由 `chat.service._rag_input` 收敛）。"""
    out: dict[str, Any] = {}
    if body.temperature is not None:
        out["temperature"] = body.temperature
    if body.max_tokens is not None:
        out["max_tokens"] = body.max_tokens
    return out


def parse_conversation_id(raw: str | None) -> UUID | None:
    """`X-AICS-Conversation-Id` 请求头（不合法 → 422，跨应用由 chat.service 判 404）。"""
    value = (raw or "").strip()
    if not value:
        return None
    try:
        return UUID(value)
    except ValueError as e:
        raise _bad("X-AICS-Conversation-Id", "会话 ID 不是合法的 UUID") from e


def completion_id() -> str:
    return f"chatcmpl-{new_id().hex}"


def created_now() -> int:
    return int(datetime.now(UTC).timestamp())


def aics_payload(r: TurnResult) -> dict[str, Any]:
    """扩展字段：原生接口的会话 / 意图 / 引用信息（开放接口参考 §3）。"""
    return {
        "message_id": r.message_id,
        "conversation_id": r.conversation_id,
        "previous_conversation_id": r.previous_conversation_id,
        "reply_by": r.reply_by,
        "handoff_status": r.handoff_status,
        "answer_type": r.answer_type,
        "handoff": r.handoff,
        "intent": r.intent,
        "citations": r.citations,
    }


def usage(r: TurnResult) -> dict[str, int]:
    return {
        "prompt_tokens": r.prompt_tokens,
        "completion_tokens": r.completion_tokens,
        "total_tokens": r.prompt_tokens + r.completion_tokens,
    }


def completion_body(r: TurnResult, *, model: str) -> dict[str, Any]:
    """阻塞应答；`reply_by=none`（坐席已接入）时 content 为空串、finish_reason 仍是 stop。"""
    return {
        "id": completion_id(),
        "object": COMPLETION_OBJECT,
        "created": created_now(),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": r.answer or ""},
                "finish_reason": "stop",
            }
        ],
        "usage": usage(r),
        "aics": aics_payload(r),
    }


def chunk(
    *,
    cid: str,
    model: str,
    created: int,
    delta: dict[str, Any],
    finish_reason: str | None = None,
    aics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": cid,
        "object": CHUNK_OBJECT,
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    if aics is not None:  # 末 chunk 带引用 / 意图 / 会话（开放接口参考 §3）
        body["aics"] = aics
    return body


def _data(payload: Any) -> str:
    return (
        f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'), default=str)}\n\n"
    )


# ---------------------------------------------------------------- OpenAI 风格错误体
_ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "invalid_request_error",
    405: "invalid_request_error",
    409: "invalid_request_error",
    413: "invalid_request_error",
    422: "invalid_request_error",
    429: "rate_limit_error",
}
_CODE_ALIASES = {
    "APP_NOT_FOUND": "model_not_found",  # `model` 与 Key 所属应用不一致（开放接口参考 §3）
    "CONVERSATION_NOT_FOUND": "conversation_not_found",
    "VALIDATION_ERROR": "invalid_request",  # 与 FastAPI 校验分支（INVALID_REQUEST）同码
}


def error_type(status: int) -> str:
    return _ERROR_TYPES.get(status, "api_error" if status >= 500 else "invalid_request_error")


def error_code(code: str) -> str:
    return _CODE_ALIASES.get(code, code.lower())


def error_body(code: str, message: str, status: int) -> dict[str, Any]:
    """`/v1/*` 的错误体：`{"error":{"message","type","code"}}`（原生接口仍是 `{"error":{...}}`）。"""
    return {"error": {"message": message, "type": error_type(status), "code": error_code(code)}}


# ---------------------------------------------------------------- 流式
async def stream(
    handle: TurnHandle,
    *,
    model: str,
    heartbeat_s: float = HEARTBEAT_S,
) -> AsyncIterator[str]:
    """`stream=true` 的响应体：每个 delta 一个 `chat.completion.chunk`，末 chunk 带 `aics`
    与 `finish_reason=stop`，随后 `data: [DONE]`；空闲沿用 SSE 注释行心跳。中途出错发一个
    带 `error` 的 chunk 再收尾（OpenAI 没有流内错误事件，客户端按 chunk 解析）。"""
    cid, created = completion_id(), created_now()
    try:
        while True:
            try:
                ev = await handle.next_event(heartbeat_s)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            if ev is None:
                return
            text = _encode(ev, cid=cid, model=model, created=created)
            if text:
                yield text
            if isinstance(ev, (TurnResult, ErrorEvent)):
                yield "data: [DONE]\n\n"
                return
    finally:
        handle.cancel()  # no-op once the answer is complete


def _encode(ev: TurnEvent, *, cid: str, model: str, created: int) -> str:
    """一个轮次事件 → 0..n 个 SSE 分片（`start` 与 `citations` 只在末 chunk 的 aics 里体现）。"""
    if isinstance(ev, StartEvent):
        return _data(
            chunk(cid=cid, model=model, created=created, delta={"role": "assistant", "content": ""})
        )
    if isinstance(ev, DeltaEvent):
        return _data(chunk(cid=cid, model=model, created=created, delta={"content": ev.text}))
    if isinstance(ev, TurnResult):
        return _data(
            chunk(
                cid=cid,
                model=model,
                created=created,
                delta={},
                finish_reason="stop",
                aics={**aics_payload(ev), "usage": usage(ev)},
            )
        )
    if isinstance(ev, ErrorEvent):
        body = error_body(ev.code, ev.message, ev.status)
        return _data(
            {
                "id": cid,
                "object": CHUNK_OBJECT,
                "created": created,
                "model": model,
                "choices": [],
                **body,
            }
        )
    return ""  # TraceEvent：调试信息不进 OpenAI 通道


__all__ = [
    "CHUNK_OBJECT",
    "COMPLETION_OBJECT",
    "LIST_OBJECT",
    "MODEL_OBJECT",
    "OWNED_BY",
    "ChatCompletionRequest",
    "ChatMessage",
    "ContentPart",
    "Parsed",
    "aics_payload",
    "chunk",
    "completion_body",
    "completion_id",
    "content_text",
    "created_now",
    "error_body",
    "error_code",
    "error_type",
    "overrides",
    "parse",
    "parse_conversation_id",
    "stream",
    "usage",
]
