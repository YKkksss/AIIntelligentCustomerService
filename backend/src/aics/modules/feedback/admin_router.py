"""反馈的运营面接口（M4.5 ③）：列表与「标记已处理」。

前缀 `/api/v1/admin/apps/{app_id}`，权限 `log.read` 读、`feedback.write` 写。
只读成员（没有 `app.write`）看到的评论与回答摘要会打码，与调试对话、会话日志同一口径。
`handled` 是运营内部状态，不改变用户已提交的评价。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict

from aics.core.errors import ForbiddenError
from aics.core.openapi import errors
from aics.core.security import AuthedUser, require_perm
from aics.modules.feedback.admin_service import FeedbackAdminService

router = APIRouter(prefix="/api/v1/admin/apps/{app_id}", tags=["admin-feedback"])

FeedbackReader = Annotated[AuthedUser, Depends(require_perm("log.read"))]
FeedbackWriter = Annotated[AuthedUser, Depends(require_perm("feedback.write"))]

FEEDBACK_ERRORS = errors(401, 403, 404, 422, 429)


class FeedbackMessage(BaseModel):
    role: str
    intent: str | None = None
    excerpt: str


class FeedbackRow(BaseModel):
    id: str
    message_id: str
    conversation_id: str | None = None
    rating: int
    comment: str | None = None
    source: str
    handled: bool
    created_at: str | None = None
    updated_at: str | None = None
    message: FeedbackMessage | None = None


class FeedbackPage(BaseModel):
    items: list[FeedbackRow]
    next_cursor: str | None = None


class HandleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    handled: bool = True


def _runtime(request: Request) -> Any:
    return request.app.state.feedback_runtime


async def _session(request: Request) -> AsyncIterator[Any]:
    async with _runtime(request).sessions() as session:
        yield session


def _service(request: Request, session: Any) -> FeedbackAdminService:
    sink = getattr(getattr(request.app.state, "audit_runtime", None), "sink", None)
    return FeedbackAdminService(session, audit=None if sink is None else sink())


def _meta(request: Request) -> dict[str, Any]:
    rid = request.scope.get("state", {}).get("request_id")
    return {
        "ip": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
        "request_id": rid,
    }


async def _read_only(request: Request, user: AuthedUser, app_id: UUID) -> bool:
    """只读成员（viewer）看到的文本打码；有 `app.write` 才算可写成员。"""
    if user.is_super_admin:
        return False
    authorizer: Any = request.app.state.authorizer
    try:
        await authorizer.authorize(user, app_id=app_id, permission="app.write")
    except ForbiddenError:
        return True
    return False


@router.get(
    "/feedback",
    summary="反馈列表（点踩/点赞，按时间倒序）",
    response_model=FeedbackPage,
    responses=FEEDBACK_ERRORS,
)
async def list_feedback(
    app_id: UUID,
    request: Request,
    user: FeedbackReader,
    session: Annotated[Any, Depends(_session)],
    rating: int | None = Query(default=None, description="1 点赞、-1 点踩"),
    handled: bool | None = None,
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
) -> FeedbackPage:
    mask = await _read_only(request, user, app_id)
    body = await _service(request, session).list(
        app_id, rating=rating, handled=handled, cursor=cursor, limit=limit, mask=mask
    )
    return FeedbackPage.model_validate(body)


@router.patch(
    "/feedback/{feedback_id}",
    summary="标记反馈已处理 / 取消已处理",
    response_model=FeedbackRow,
    responses=FEEDBACK_ERRORS,
)
async def handle_feedback(
    app_id: UUID,
    feedback_id: UUID,
    body: HandleBody,
    request: Request,
    user: FeedbackWriter,
    session: Annotated[Any, Depends(_session)],
) -> FeedbackRow:
    row = await _service(request, session).handle(
        app_id, feedback_id, handled=body.handled, actor_id=user.id, meta=_meta(request)
    )
    return FeedbackRow.model_validate(row)
