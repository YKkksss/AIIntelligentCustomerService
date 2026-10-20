"""调试对话（M4.4 ②，ADR-0019）与会话日志 / 待回访（M4.5 ③）。

- `POST /debug/chat`：SSE；`channel=debug`、`run_mode=debug` 由路由写死；`debug_config`
  只作用本轮（校验失败在开流之前 422，`details.errors` 给出字段路径）。
- `GET /debug/conversations/{conversation_id}`：会话与消息（含每条应答的 trace）。
- `POST /debug/compact`：立即压缩上下文，写 `jobs(type=compact)`。

前缀 `/api/v1/admin/apps/{app_id}`，权限 `chat.debug`。viewer 也有这个权限，但 trace 与
消息正文会脱敏（手机号打码）——有 `app.write` 才能看到原文。
SSE 帧编码复用 `modules.chat.sse`（ISSUE-82）。`/conversations*` 与 `/callbacks*` 是运营面：`log.read` 读、`handoff.release` / `callback.clear` 写，viewer 的正文与 trace 打码。
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from aics.core.errors import ForbiddenError
from aics.core.openapi import errors
from aics.core.security import AuthedUser, require_perm
from aics.modules.chat import sse
from aics.modules.chat.debug import DEBUG_CHANNEL, DebugChat
from aics.modules.chat.logs import DEFAULT_CHANNELS, ConversationLogs, LogFilter
from aics.modules.chat.runtime import ChatRuntime
from aics.modules.chat.schemas import QUESTION_MAX_CHARS
from aics.modules.chat.service import ChatService, TurnRequest
from aics.rag.schemas import RunMode

router = APIRouter(prefix="/api/v1/admin/apps/{app_id}", tags=["admin-chat"])

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


# --------------------------------------------------------------------------- 会话日志（M4.5 ③）

LOG_ERRORS = errors(401, 403, 404, 422, 429)

LogReader = Annotated[AuthedUser, Depends(require_perm("log.read"))]
ReleaseUser = Annotated[AuthedUser, Depends(require_perm("handoff.release"))]
CallbackUser = Annotated[AuthedUser, Depends(require_perm("callback.clear"))]


class ConversationRow(BaseModel):
    id: str
    channel: str
    status: str
    title: str | None = None
    external_user_id: str | None = None
    created_at: str | None = None
    last_activity_at: str | None = None
    closed_at: str | None = None
    close_reason: str | None = None
    message_count: int
    handoff_status: str
    handoff_reason: str | None = None
    handoff_requested_at: str | None = None
    handoff_end_reason: str | None = None
    handoff_agent_name: str | None = None
    needs_callback: bool
    callback_reason: str | None = None
    callback_requested_at: str | None = None
    callback_cleared_at: str | None = None
    callback_note: str | None = None
    intent: str | None = None
    downvotes: int


class ConversationPage(BaseModel):
    items: list[ConversationRow]
    next_cursor: str | None = None


class LogMessage(BaseModel):
    id: str
    role: str
    kind: str | None = None
    agent_id: str | None = None
    content: str
    created_at: str | None = None
    answer_type: str | None = None
    intent: str | None = None
    handoff: bool | None = None
    degraded: bool | None = None
    citations: list[Any] | None = None


class LogSummary(BaseModel):
    value: dict[str, Any]
    version: int
    until_msg_id: str | None = None
    updated_at: str | None = None


class LogFact(BaseModel):
    key: str
    value: Any
    category: str | None = None
    confidence: float | None = None
    version: int
    first_seen_at: str | None = None


class ConversationDetail(BaseModel):
    conversation: ConversationRow
    messages: list[LogMessage]
    summary: LogSummary | None = None
    facts: list[LogFact]


class NoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(default=None, max_length=500)


async def _session(request: Request) -> AsyncIterator[Any]:
    """管理侧读日志/改状态用短会话，与请求同生命周期。"""
    async with _runtime(request).sessions() as session:
        yield session


def _logs(request: Request, session: Any) -> ConversationLogs:
    runtime = _runtime(request)
    sink = getattr(getattr(request.app.state, "audit_runtime", None), "sink", None)
    return ConversationLogs(
        session,
        redis=runtime.redis,
        redis_prefix=runtime.redis_prefix,
        audit=None if sink is None else sink(),
    )


def _meta(request: Request) -> dict[str, Any]:
    return {
        "ip": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
        "request_id": _rid(request),
    }


def _csv(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


@router.get(
    "/conversations",
    summary="会话日志（筛选 + 游标分页）",
    response_model=ConversationPage,
    responses=LOG_ERRORS,
)
async def list_conversations(
    app_id: UUID,
    request: Request,
    user: LogReader,
    session: Annotated[Any, Depends(_session)],
    channel: str | None = Query(default=None, description="逗号分隔；缺省 api,openai"),
    status: str | None = None,
    handoff_status: str | None = None,
    close_reason: str | None = None,
    needs_callback: bool | None = None,
    intent: str | None = None,
    downvoted: bool | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    q: str | None = Query(default=None, max_length=200),
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
) -> ConversationPage:
    channels = _csv(channel) or DEFAULT_CHANNELS
    filters = LogFilter(
        channels=channels,
        status=status,
        handoff_status=handoff_status,
        close_reason=close_reason,
        needs_callback=needs_callback,
        intents=_csv(intent),
        downvoted=downvoted,
        since=since,
        until=until,
        q=q.strip() if q and q.strip() else None,
    )
    body = await _logs(request, session).list(app_id, filters=filters, cursor=cursor, limit=limit)
    return ConversationPage.model_validate(body)


@router.get(
    "/conversations/{conversation_id}",
    summary="会话详情（消息时间线、引用、trace、摘要与事实）",
    response_model=ConversationDetail,
    responses=LOG_ERRORS,
)
async def conversation_detail(
    app_id: UUID,
    conversation_id: UUID,
    request: Request,
    user: LogReader,
    session: Annotated[Any, Depends(_session)],
) -> ConversationDetail:
    mask = await _read_only(request, user, app_id)
    body = await _logs(request, session).detail(app_id, conversation_id, mask=mask)
    return ConversationDetail.model_validate(body)


@router.post(
    "/conversations/{conversation_id}/release",
    summary="后台强制释放转人工会话",
    response_model=ConversationRow,
    responses=LOG_ERRORS,
)
async def release_conversation(
    app_id: UUID,
    conversation_id: UUID,
    body: NoteBody,
    request: Request,
    user: ReleaseUser,
    session: Annotated[Any, Depends(_session)],
) -> ConversationRow:
    row = await _logs(request, session).force_release(
        app_id,
        conversation_id,
        actor_id=user.id,
        note=body.note,
        meta=_meta(request),
    )
    return ConversationRow.model_validate(row)


@router.get(
    "/callbacks",
    summary="待回访会话",
    response_model=ConversationPage,
    responses=LOG_ERRORS,
)
async def list_callbacks(
    app_id: UUID,
    request: Request,
    user: LogReader,
    session: Annotated[Any, Depends(_session)],
    include_cleared: bool = False,
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
) -> ConversationPage:
    body = await _logs(request, session).callbacks(
        app_id, cursor=cursor, limit=limit, include_cleared=include_cleared
    )
    return ConversationPage.model_validate(body)


@router.post(
    "/callbacks/{conversation_id}",
    summary="标记已回访",
    response_model=ConversationRow,
    responses=LOG_ERRORS,
)
async def clear_callback(
    app_id: UUID,
    conversation_id: UUID,
    body: NoteBody,
    request: Request,
    user: CallbackUser,
    session: Annotated[Any, Depends(_session)],
) -> ConversationRow:
    row = await _logs(request, session).clear_callback(
        app_id, conversation_id, note=body.note, actor_id=user.id, meta=_meta(request)
    )
    return ConversationRow.model_validate(row)
