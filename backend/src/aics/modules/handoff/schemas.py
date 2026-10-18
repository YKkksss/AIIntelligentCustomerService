"""坐席接口的请求体（M3.4 ②，开放接口参考 §5）。"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

#: 坐席发言长度：开放接口参考 §5 写 1–4000 字（比 `MESSAGE_CONTENT_MAX_CHARS` 更紧）。
AGENT_MESSAGE_MAX_CHARS = 4000
AGENT_ID_MAX_CHARS = 128
AGENT_NAME_MAX_CHARS = 64

AgentId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=AGENT_ID_MAX_CHARS),
]
AgentName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=AGENT_NAME_MAX_CHARS),
]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AcceptBody(_Body):
    """`POST …/handoff/accept`：抢单接入（`pending → accepted`）。"""

    agent_id: AgentId
    agent_name: AgentName | None = None


class AgentMessageBody(_Body):
    """`POST …/handoff/messages`：坐席发言（仅接入的坐席本人）。"""

    agent_id: AgentId
    content: str = Field(min_length=1, max_length=AGENT_MESSAGE_MAX_CHARS)


class ReleaseBody(_Body):
    """`POST …/handoff/release`：交还 AI（`pending|accepted → released`）。"""

    agent_id: AgentId
    note: str | None = Field(default=None, max_length=500)
