"""调试对话接口（M4.4 ②，ADR-0019）。

- `POST /debug/chat`：SSE；`channel=debug`、`run_mode=debug` 由路由写死；`debug_config`
  只作用本轮（校验失败在开流之前 422，`details.errors` 给出字段路径）。
- `GET /debug/conversations/{conversation_id}`：会话与消息（含每条应答的 trace）。
- `POST /debug/compact`：立即压缩上下文，写 `jobs(type=compact)`。

前缀 `/api/v1/admin/apps/{app_id}`，权限 `chat.debug`。viewer 也有这个权限，但 trace 与
消息正文会脱敏（手机号打码）——有 `app.write` 才能看到原文。
SSE 帧编码复用 `modules.chat.sse`（ISSUE-82）。
"""

from __future__ import annotations

import time
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from aics.core.errors import ForbiddenError
from aics.core.openapi import errors
from aics.core.security import AuthedUser, require_perm
from aics.modules.chat import sse
from aics.modules.chat.debug import DEBUG_CHANNEL, DebugChat
from aics.modules.chat.runtime import ChatRuntime
from aics.modules.chat.schemas import QUESTION_MAX_CHARS
from aics.modules.chat.service import ChatService, TurnRequest
from aics.rag.schemas import RunMode

router = APIRouter(prefix="/api/v1/admin/apps/{app_id}", tags=["admin-debug"])

DebugUser = Annotated[AuthedUser, Depends(require_perm("chat.debug"))]

# 401/403 鉴权、404 应用或会话不存在、422 参数/调试配置、429 限流、500 压缩组件异常
DEBUG_ERRORS = errors(401, 403, 404, 422, 429, 500)


class DebugTurnBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=QUESTION_MAX_CHARS)
    conversation_id: UUID | None = None
    debug_config: dict[str, Any] | None = None


class CompactBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID


def _runtime(request: Request) -> ChatRuntime:
    return request.app.state.chat_runtime


def _rid(request: Request) -> str | None:
    return request.scope.get("state", {}).get("request_id")


def _stream_headers() -> dict[str, str]:
    return {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


async def _read_only(request: Request, user: AuthedUser, app_id: UUID) -> bool:
    """只读成员（viewer）的 trace 与正文打码；有 `app.write` 视为可写成员。"""
    if user.is_super_admin:
        return False
    authorizer: Any = request.app.state.authorizer
    try:
        await authorizer.authorize(user, app_id=app_id, permission="app.write")
    except ForbiddenError:
        return True
    return False


@router.post("/debug/chat", summary="调试对话（SSE，channel=debug）", responses=DEBUG_ERRORS)
async def debug_chat(
    app_id: UUID, body: DebugTurnBody, request: Request, user: DebugUser
) -> StreamingResponse:
    t0 = time.perf_counter()
    svc = ChatService(_runtime(request))
    handle = await svc.start_turn(
        TurnRequest(
            app_id=app_id,
            question=body.question.strip(),
            conversation_id=body.conversation_id,
            channel=DEBUG_CHANNEL,
            run_mode=RunMode.debug,
            created_by=user.id,
            debug_config=body.debug_config or {},
        ),
        t0=t0,
    )
    return StreamingResponse(
        sse.stream(handle, _rid(request)),
        media_type="text/event-stream",
        headers=_stream_headers(),
    )


@router.get(
    "/debug/conversations/{conversation_id}",
    summary="调试会话与消息（含 trace，只读成员脱敏）",
    responses=DEBUG_ERRORS,
)
async def debug_history(
    app_id: UUID, conversation_id: UUID, request: Request, user: DebugUser
) -> JSONResponse:
    mask = await _read_only(request, user, app_id)
    body = await DebugChat(_runtime(request)).history(
        app_id=app_id, conversation_id=conversation_id, mask=mask
    )
    return JSONResponse(jsonable_encoder(body))


@router.post("/debug/compact", summary="立即压缩会话上下文", responses=DEBUG_ERRORS)
async def debug_compact(
    app_id: UUID, body: CompactBody, request: Request, user: DebugUser
) -> JSONResponse:
    result = await DebugChat(_runtime(request)).compact(
        app_id=app_id, conversation_id=body.conversation_id, created_by=user.id
    )
    return JSONResponse(jsonable_encoder(result))
