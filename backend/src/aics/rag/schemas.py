"""run_rag input / output types (RAG 流程设计 §2, 意图识别 §10).

`RagInput` follows §2 and adds the app-level settings the prompts and branches need
(`profile`, `handoff`) plus the request tag filter, so rag never reads the apps table
(design discussion 0004 §2).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from aics.rag.config import AppRagConfig, HandoffConfig
from aics.rag.ports import ChatTurn

AnswerType = Literal["answer", "refused", "handoff"]
FinalIntent = Literal["kb_qa", "chitchat", "human", "out_of_scope", "action", "clarify", "blocked"]
IntentSource = Literal["rule", "vector", "llm", "fallback"]

QUESTION_MAX_CHARS = 4000  # 开放接口单条消息上限（接口规范），rag 再兜底一次


class RunMode(StrEnum):
    online = "online"  # open API / OpenAI-compatible
    debug = "debug"  # admin debug chat (trace events are streamed)
    eval = "eval"  # evaluation runs


class AppProfile(BaseModel):
    """App columns used by prompts and fixed replies (apps table, 数据库设计 §3.4)."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, max_length=100)
    description: str | None = None
    greeting: str | None = None  # apps.greeting; default chitchat greeting reply
    system_prompt: str | None = None
    answer_style: str | None = None
    fallback_text: str = Field(min_length=1, max_length=500)
    language: str = "zh-CN"


class RagInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    app_id: UUID
    conversation_id: UUID | None = None  # None = stateless (OpenAI-compatible without header)
    question: str = Field(min_length=1, max_length=QUESTION_MAX_CHARS)
    inline_history: list[ChatTurn] | None = None
    config: AppRagConfig = Field(default_factory=AppRagConfig)  # overrides already merged
    profile: AppProfile
    handoff: HandoffConfig = Field(default_factory=HandoffConfig)
    tags: list[str] | None = None  # request tag filter; None → config.default_tags
    refusal_streak: int = Field(0, ge=0)
    last_assistant_was_clarify: bool = False
    run_mode: RunMode = RunMode.online

    @field_validator("question")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("question must not be blank")
        return v


class Citation(BaseModel):
    """One cited source; `index` is the `[n]` marker in the answer (1-based)."""

    index: int = Field(ge=1)
    chunk_id: UUID
    parent_id: UUID | None = None
    document_id: UUID
    document_title: str
    heading_path: str | None = None
    page: int | None = None
    snippet: str = ""
    rerank_score: float | None = None


class IntentInfo(BaseModel):
    name: FinalIntent = "kb_qa"
    confidence: float = Field(1.0, ge=0, le=1)
    source: IntentSource = "fallback"


class TokenUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0  # subset of completion_tokens (reasoning models)
    calls: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def add(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            calls=self.calls + other.calls,
        )


class RagResult(BaseModel):
    """The single output of rag; chat.service persists it (RAG 流程设计 §2)."""

    kind: Literal["result"] = "result"
    answer: str
    answer_type: AnswerType
    handoff_reason: str | None = None
    decision_reason: str
    intent: IntentInfo = Field(default_factory=IntentInfo)
    slots: dict[str, str | None] = Field(default_factory=dict)
    rewritten_query: str | None = None
    citations: list[Citation] = Field(default_factory=list)
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    prompt_version: str = ""  # e.g. "cc1.gen1.judge1" → messages.prompt_version
    llm_model: str = ""
    usage: TokenUsage = Field(default_factory=TokenUsage)
    timings: dict[str, int] = Field(default_factory=dict)
    degraded: list[str] = Field(default_factory=list)
    context_usage: dict[str, int] = Field(default_factory=dict)
    trace: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------- stream events (§6)
class CitationsEvent(BaseModel):
    """Candidates sent to generation; emitted once, before the first delta (kb_qa answer)."""

    kind: Literal["citations"] = "citations"
    citations: list[Citation]


class DeltaEvent(BaseModel):
    kind: Literal["delta"] = "delta"
    text: str


class TraceEvent(BaseModel):
    """Debug-only trace snapshot (RunMode.debug)."""

    kind: Literal["trace"] = "trace"
    trace: dict[str, Any]


RagEvent = CitationsEvent | DeltaEvent | TraceEvent | RagResult
