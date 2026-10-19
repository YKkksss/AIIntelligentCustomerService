"""反馈请求体（M3.6 ②，开放接口参考 §2 的最后一条）。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

#: 反馈评论长度：开放接口参考 §2 写 ≤2000 字；空串视作没写评论。
COMMENT_MAX_CHARS = 2000

Comment = Annotated[str, StringConstraints(strip_whitespace=True, max_length=COMMENT_MAX_CHARS)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FeedbackBody(_Body):
    """`POST …/messages/{message_id}/feedback`：点赞 `1` / 点踩 `-1`，评论可选。

    `rating` 用 `Literal[1, -1]` 而不是 `int` + 范围校验：越界在 pydantic 层就是 422
    `VALIDATION_ERROR`，与数据库的 CHECK 双向对齐、错误体也不用自己拼。
    """

    rating: Literal[1, -1]
    comment: Comment | None = Field(default=None)
