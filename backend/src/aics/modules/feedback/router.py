"""反馈接口路由（M3.6 ②，开放接口参考 §2）。

挂在 `/api/v1/apps/{app_id}` 下，scope 用 `chat`：反馈是对话侧的动作，调用方拿的就是对话
Key，不该为点赞单独发一个 `feedback:write`。响应只回 `{id, rating}`——反馈是「记账」，调用方
不需要拿回评论原文。
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from aics.core.security import Principal, require_scope
from aics.modules.feedback.runtime import FeedbackRuntime
from aics.modules.feedback.schemas import FeedbackBody

router = APIRouter(prefix="/api/v1/apps/{app_id}", tags=["feedback"])

ChatScope = Annotated[Principal, Depends(require_scope("chat"))]


def _runtime(request: Request) -> FeedbackRuntime:
    return request.app.state.feedback_runtime


def _body(row: Any) -> dict[str, Any]:
    return {"id": str(row.id), "rating": row.rating}


@router.post("/messages/{message_id}/feedback")
async def put_feedback(
    app_id: UUID,
    message_id: UUID,
    body: FeedbackBody,
    request: Request,
    principal: ChatScope,
) -> JSONResponse:
    """点赞 / 点踩一条 assistant 回答；同一消息重复提交按覆盖写（`id` 不变）。"""
    rt = _runtime(request)
    async with rt.session() as session:
        row = await rt.service(session).submit(
            app_id, message_id, rating=body.rating, comment=body.comment
        )
    return JSONResponse(_body(row), headers=principal.rate_headers)
