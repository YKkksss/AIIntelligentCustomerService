"""OpenAI 兼容端点（开放接口参考 §3；M3.3 ③）。

与原生 `/api/v1/apps/{app_id}/chat` 共用 `ChatService`，区别是：
- 没有路径里的 `app_id`：应用身份来自 API Key（`app_id = principal.app_id`），`model` 必须等于它；
- 请求里的 `messages` 就是历史（无状态），只有带 `X-AICS-Conversation-Id` 时才用服务端会话；
- 应答与错误体是 OpenAI 形状（`{"error":{"message","type","code"}}`，见 `main.py` 的 `/v1` 分流）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse

from aics.core.errors import AppNotFoundError
from aics.core.security import Principal, require_key_scope
from aics.modules.chat import openai
from aics.modules.chat.service import ChatService, TurnRequest

router = APIRouter(prefix="/v1", tags=["openai"])

MODEL_MISMATCH = "model 与 API Key 所属应用不一致"


def _service(request: Request) -> ChatService:
    return ChatService(request.app.state.chat_runtime)


def _same_app(model: str, principal: Principal) -> str:
    """`model` 只能是 Key 所属应用的 id（别的应用 → 404 model_not_found）。"""
    if model.strip() != str(principal.app_id):
        raise AppNotFoundError(message=MODEL_MISMATCH)
    return str(principal.app_id)


@router.get("/models")
async def list_models(
    principal: Annotated[Principal, Depends(require_key_scope("chat"))],
) -> JSONResponse:
    """只返回当前 Key 能用的模型：它所属的应用 id（不暴露模型注册表）。"""
    body = {
        "object": openai.LIST_OBJECT,
        "data": [
            {
                "id": str(principal.app_id),
                "object": openai.MODEL_OBJECT,
                "owned_by": openai.OWNED_BY,
            }
        ],
    }
    return JSONResponse(body, headers=principal.rate_headers)


@router.post("/chat/completions", response_model=None)
async def chat_completions(
    request: Request,
    body: openai.ChatCompletionRequest,
    principal: Annotated[Principal, Depends(require_key_scope("chat"))],
    x_aics_conversation_id: Annotated[str | None, Header(alias="X-AICS-Conversation-Id")] = None,
) -> JSONResponse | StreamingResponse:
    """阻塞与流式共用一条路径：`stream=true` 走 SSE，每个 delta 一个 `chat.completion.chunk`。"""
    model = _same_app(body.model, principal)
    parsed = openai.parse(body)
    req = TurnRequest(
        app_id=principal.app_id,
        question=parsed.question,
        conversation_id=openai.parse_conversation_id(x_aics_conversation_id),
        external_user_id=parsed.user,
        channel="openai",
        api_key_id=principal.key_id,
        inline_history=parsed.history or None,
        rag_overrides=openai.overrides(body),
    )
    handle = await _service(request).start_turn(req)
    if not body.stream:
        result = await handle.result()
        # `aics` 里带 UUID（会话 / 消息 id）：走 jsonable_encoder，与其它路由一致。
        return JSONResponse(
            jsonable_encoder(openai.completion_body(result, model=model)),
            headers=principal.rate_headers,
        )
    headers = dict(principal.rate_headers)
    headers["Cache-Control"] = "no-cache"
    headers["X-Accel-Buffering"] = "no"
    return StreamingResponse(
        openai.stream(handle, model=model),
        media_type="text/event-stream",
        headers=headers,
    )


__all__ = ["router"]
