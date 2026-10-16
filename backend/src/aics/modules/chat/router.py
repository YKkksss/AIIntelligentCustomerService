"""Open-API chat routes (开放接口参考 §1).

- `POST /api/v1/apps/{app_id}/chat` (scope `chat`): blocking JSON or SSE (`stream=true`).
- `GET /api/v1/apps/{app_id}/conversations/{conversation_id}/messages`
  (scope `chat` or `handoff:write`): cursor-paged messages + the conversation object.

Errors raised before the turn starts (404 / 422 / 401 / 429) are plain JSON error bodies even
for `stream=true`; failures during generation become `event: error` (SSE) or the error body
(blocking). The ChatRuntime is installed on `app.state.chat_runtime` by `aics.main`.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import time
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse

from aics.core.errors import ValidationAppError
from aics.core.security import Principal, require_scope
from aics.db.models import Message
from aics.modules.chat import sse
from aics.modules.chat.repository import ConversationRepository, MessageRepository
from aics.modules.chat.schemas import ChatRequest
from aics.modules.chat.service import ChatService, TurnRequest

router = APIRouter(prefix="/api/v1/apps/{app_id}", tags=["chat"])

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100  # 接口规范 §5


def _rid(request: Request) -> str | None:
    return request.scope.get("state", {}).get("request_id")


@router.post("/chat")
async def chat(
    app_id: UUID,
    body: ChatRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require_scope("chat"))],
) -> Any:
    t0 = time.perf_counter()
    svc = ChatService(request.app.state.chat_runtime)
    handle = await svc.start_turn(
        TurnRequest(
            app_id=app_id,
            question=body.question,
            conversation_id=body.conversation_id,
            external_user_id=body.user_id,
            channel="api",
            metadata=body.metadata,
            api_key_id=principal.key_id,
        ),
        t0=t0,
    )
    headers = dict(principal.rate_headers)
    if body.stream:
        headers.update({"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        return StreamingResponse(
            sse.stream(handle, _rid(request)), media_type="text/event-stream", headers=headers
        )
    try:
        result = await handle.result()
    except asyncio.CancelledError:
        handle.cancel()
        raise
    return JSONResponse(jsonable_encoder(sse.blocking_body(result)), headers=headers)


def _encode_cursor(message_id: UUID) -> str:
    return base64.urlsafe_b64encode(f"b:{message_id}".encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> UUID:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        kind, _, value = raw.partition(":")
        if kind != "b":
            raise ValueError(kind)
        return UUID(value)
    except (ValueError, binascii.Error, UnicodeDecodeError) as e:
        raise ValidationAppError(
            details={"errors": [{"loc": ["query", "cursor"], "msg": "无效的游标"}]}
        ) from e


def message_object(m: Message) -> dict[str, Any]:
    return {
        "id": m.id,
        "role": m.role,
        "kind": m.kind,
        "content": m.content,
        "answer_type": m.answer_type,
        "handoff": m.handoff,
        "intent": (
            {"name": m.intent, "confidence": m.intent_confidence, "source": m.intent_source}
            if m.intent
            else None
        ),
        "citations": m.citations,
        "agent_id": m.agent_id,
        "interrupted": m.interrupted,
        "created_at": m.created_at,
    }


@router.get("/conversations/{conversation_id}/messages")
async def list_messages(
    app_id: UUID,
    conversation_id: UUID,
    request: Request,
    principal: Annotated[Principal, Depends(require_scope("chat", "handoff:write"))],
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> Any:
    before = _decode_cursor(cursor) if cursor else None
    async with request.app.state.chat_runtime.sessions() as s:
        conv = await ConversationRepository(s).get_or_404(app_id, conversation_id)
        rows, more = await MessageRepository(s).page_for_conversation(
            app_id, conversation_id, limit=limit, before=before
        )
    body = {
        "items": [message_object(m) for m in rows],
        "next_cursor": _encode_cursor(rows[0].id) if more and rows else None,
        "conversation": {
            "id": conv.id,
            "status": conv.status,
            "handoff_status": conv.handoff_status,
            "closed_at": conv.closed_at,
            "previous_conversation_id": conv.previous_conversation_id,
        },
    }
    return JSONResponse(jsonable_encoder(body), headers=principal.rate_headers)
