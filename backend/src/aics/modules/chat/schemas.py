"""Validated inputs for writing conversations and messages (M2.1).

The repository only accepts these models, so every insert path goes through the same checks
as the database CHECK constraints (fail fast with VALIDATION_ERROR instead of IntegrityError).
"""

from __future__ import annotations

import json
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aics.db.models.conversation import ANSWER_TYPES, INTENT_SOURCES, INTENTS, MESSAGE_KINDS

# Storage-level upper bound for one message body (characters). The open API limits a question
# to 2000 characters; assistant answers are bounded by max_tokens. This only guards the table
# against runaway payloads (参数默认值与依据: MESSAGE_CONTENT_MAX_CHARS).
MESSAGE_CONTENT_MAX_CHARS = 20_000
# Conversation title = first user question truncated to this many characters (数据库设计 §3.8).
TITLE_MAX_CHARS = 50
# Open API: user_id ≤ 128 characters (开放接口参考 §chat).
EXTERNAL_USER_ID_MAX_CHARS = 128

Channel = Literal["api", "openai", "debug", "eval"]
Role = Literal["user", "assistant", "human_agent", "system"]
Kind = Literal[MESSAGE_KINDS]  # type: ignore[valid-type]
AnswerType = Literal[ANSWER_TYPES]  # type: ignore[valid-type]
Intent = Literal[INTENTS]  # type: ignore[valid-type]
IntentSource = Literal[INTENT_SOURCES]  # type: ignore[valid-type]

_ASSISTANT_ONLY = (
    "answer_type",
    "intent",
    "intent_confidence",
    "intent_source",
    "slots",
    "rewritten_query",
    "citations",
    "retrieval_trace",
    "decision_reason",
    "prompt_tokens",
    "completion_tokens",
    "latency_ms",
    "first_token_ms",
    "llm_model",
    "prompt_version",
)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NewConversation(_Model):
    channel: Channel
    external_user_id: str | None = Field(default=None, min_length=1, max_length=128)
    api_key_id: UUID | None = None
    created_by: UUID | None = None
    previous_conversation_id: UUID | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class NewMessage(_Model):
    """One message to persist. `token_count` / `search_text` are computed by the repository."""

    role: Role
    content: str = Field(max_length=MESSAGE_CONTENT_MAX_CHARS)
    kind: Kind = "chat"
    agent_id: str | None = Field(default=None, min_length=1, max_length=128)
    # ---- assistant only ----
    answer_type: AnswerType | None = None
    intent: Intent | None = None
    intent_confidence: float | None = Field(default=None, ge=0, le=1)
    intent_source: IntentSource | None = None
    slots: dict[str, Any] | None = None
    rewritten_query: str | None = None
    citations: list[dict[str, Any]] | None = None
    retrieval_trace: dict[str, Any] | None = None
    decision_reason: str | None = None
    handoff: bool = False
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    latency_ms: int | None = Field(default=None, ge=0)
    first_token_ms: int | None = Field(default=None, ge=0)
    llm_model: str | None = None
    prompt_version: str | None = None
    # ---- any role ----
    degraded: list[str] = Field(default_factory=list)
    config_version: int | None = Field(default=None, ge=1)
    interrupted: bool = False

    @model_validator(mode="after")
    def _role_rules(self) -> NewMessage:
        if self.role == "system":
            if self.kind == "chat":
                raise ValueError("system messages need a non-chat kind")
        elif self.kind != "chat":
            raise ValueError(f"kind={self.kind} is only allowed for system messages")
        if (self.agent_id is not None) != (self.role == "human_agent"):
            raise ValueError("agent_id is required for human_agent messages and only for them")
        if self.role != "assistant":
            extra = [f for f in _ASSISTANT_ONLY if getattr(self, f) is not None]
            if self.handoff:
                extra.append("handoff")
            if extra:
                raise ValueError(f"assistant-only fields set on {self.role}: {sorted(extra)}")
        elif self.answer_type is None:
            raise ValueError("assistant messages need answer_type")
        # An empty body is only legitimate for an assistant turn that failed or was cut off,
        # or for a handoff reply where the AI stays silent (reply_by=none), and for the
        # `idle_closed` timeline marker (会话生命周期 §2.2: content is empty).
        if not self.content.strip() and not (
            (
                self.role == "assistant"
                and (self.interrupted or self.answer_type in ("error", "handoff"))
            )
            or (self.role == "system" and self.kind == "idle_closed")
        ):
            raise ValueError("content must not be empty")
        return self


# ---------------------------------------------------------------- open API (开放接口参考 §1)
QUESTION_MAX_CHARS = 2000
REQUEST_METADATA_MAX_BYTES = 4096


class ChatRequest(BaseModel):
    """`POST /api/v1/apps/{app_id}/chat` body. Unknown fields are ignored (forward compatible)."""

    model_config = ConfigDict(extra="ignore")

    question: str = Field(min_length=1, max_length=QUESTION_MAX_CHARS)
    conversation_id: UUID | None = None
    user_id: str | None = Field(default=None, min_length=1, max_length=EXTERNAL_USER_ID_MAX_CHARS)
    stream: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _checks(self) -> ChatRequest:
        if not self.question.strip():
            raise ValueError("question must not be blank")
        size = len(json.dumps(self.metadata, ensure_ascii=False).encode())
        if size > REQUEST_METADATA_MAX_BYTES:
            raise ValueError(f"metadata exceeds {REQUEST_METADATA_MAX_BYTES} bytes ({size})")
        return self
