"""坐席接口路由（M3.4 ②，开放接口参考 §5）。

挂在 `/api/v1/apps/{app_id}` 下：写操作用 `handoff:write`（坐席平台单独建 Key），状态查询额外
接受 `chat`（对话侧 Key 也能看自己会话的转人工状态）。消息对象复用 `chat.router.message_object`
——两个对外开放接口描述同一行消息，形状必须一致。
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import asdict
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from aics.core.errors import ValidationAppError
from aics.core.openapi import APP_SCOPE_ERRORS, errors
from aics.core.security import Principal, require_scope
from aics.modules.chat.router import message_object
from aics.modules.handoff.runtime import HandoffRuntime
from aics.modules.handoff.schemas import AcceptBody, AgentMessageBody, ReleaseBody
from aics.modules.handoff.service import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    HandoffItem,
    HandoffState,
)

router = APIRouter(prefix="/api/v1/apps/{app_id}", tags=["handoff"])

ReadScope = Annotated[Principal, Depends(require_scope("chat", "handoff:write"))]
WriteScope = Annotated[Principal, Depends(require_scope("handoff:write"))]


def _runtime(request: Request) -> HandoffRuntime:
    return request.app.state.handoff_runtime


def _encode_cursor(conversation_id: UUID) -> str:
    return base64.urlsafe_b64encode(f"h:{conversation_id}".encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> UUID:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        kind, _, value = raw.partition(":")
        if kind != "h":
            raise ValueError(kind)
        return UUID(value)
    except (ValueError, binascii.Error, UnicodeDecodeError) as e:
        raise ValidationAppError(
            details={"errors": [{"loc": ["query", "cursor"], "msg": "无效的游标"}]}
        ) from e


def _state_body(state: HandoffState) -> dict[str, Any]:
    return jsonable_encoder(asdict(state))


def _item_body(item: HandoffItem) -> dict[str, Any]:
    body = jsonable_encoder(asdict(item))
    # 列表项的键用 `id`（与消息列表、文档列表一致），服务层内部叫 conversation_id。
    body["id"] = body.pop("conversation_id")
    return body


@router.get(
    "/conversations/{conversation_id}/handoff",
    summary="查询会话的转人工状态",
    responses=errors(*APP_SCOPE_ERRORS),
)
async def get_handoff(
    app_id: UUID,
    conversation_id: UUID,
    request: Request,
    principal: ReadScope,
) -> JSONResponse:
    """转人工状态（§3.3）：已关闭的会话也能读，`handoff_status` 保留最后一个值。"""
    rt = _runtime(request)
    async with rt.session() as session:
        state = await rt.service(session).state(app_id, conversation_id)
    return JSONResponse(_state_body(state), headers=principal.rate_headers)


@router.post(
    "/conversations/{conversation_id}/handoff/accept",
    summary="坐席接入（并发只有一个成功）",
    responses=errors(*APP_SCOPE_ERRORS, 409),
)
async def accept_handoff(
    app_id: UUID,
    conversation_id: UUID,
    body: AcceptBody,
    request: Request,
    principal: WriteScope,
) -> JSONResponse:
    """抢单接入（`pending → accepted`）；被别人抢先 → 409 `HANDOFF_STATE_CONFLICT`。"""
    rt = _runtime(request)
    async with rt.session() as session:
        state = await rt.service(session).accept(
            app_id, conversation_id, agent_id=body.agent_id, agent_name=body.agent_name
        )
    return JSONResponse(_state_body(state), headers=principal.rate_headers)


@router.post(
    "/conversations/{conversation_id}/handoff/messages",
    status_code=201,
    summary="坐席发送人工消息",
    responses=errors(*APP_SCOPE_ERRORS, 409),
)
async def post_agent_message(
    app_id: UUID,
    conversation_id: UUID,
    body: AgentMessageBody,
    request: Request,
    principal: WriteScope,
) -> JSONResponse:
    """坐席发言：写 `role=human_agent` 消息，返回消息对象（201）。"""
    rt = _runtime(request)
    async with rt.session() as session:
        msg = await rt.service(session).post_message(
            app_id, conversation_id, agent_id=body.agent_id, content=body.content
        )
    return JSONResponse(
        jsonable_encoder(message_object(msg)), status_code=201, headers=principal.rate_headers
    )


@router.post(
    "/conversations/{conversation_id}/handoff/release",
    summary="坐席交还（AI 恢复接管）",
    responses=errors(*APP_SCOPE_ERRORS, 409),
)
async def release_handoff(
    app_id: UUID,
    conversation_id: UUID,
    body: ReleaseBody,
    request: Request,
    principal: WriteScope,
) -> JSONResponse:
    """交还 AI（`pending|accepted → released`）：下一轮起 AI 恢复应答。"""
    rt = _runtime(request)
    async with rt.session() as session:
        state = await rt.service(session).release(
            app_id, conversation_id, agent_id=body.agent_id, note=body.note
        )
    return JSONResponse(_state_body(state), headers=principal.rate_headers)


@router.get(
    "/handoffs",
    summary="转人工队列（按状态分页，id 升序游标）",
    responses=errors(*APP_SCOPE_ERRORS),
)
async def list_handoffs(
    app_id: UUID,
    request: Request,
    principal: WriteScope,
    status: Annotated[Literal["pending", "accepted"], Query()] = "pending",
    cursor: Annotated[str | None, Query()] = None,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> JSONResponse:
    """坐席平台补偿拉取：`pending` / `accepted` 的会话，`id` 升序游标分页。"""
    after = _decode_cursor(cursor) if cursor else None
    rt = _runtime(request)
    async with rt.session() as session:
        items, more = await rt.service(session).list_open(
            app_id, status=status, limit=page_size, after=after
        )
    body = {
        "items": [_item_body(i) for i in items],
        "next_cursor": _encode_cursor(items[-1].conversation_id) if more and items else None,
    }
    return JSONResponse(jsonable_encoder(body), headers=principal.rate_headers)
