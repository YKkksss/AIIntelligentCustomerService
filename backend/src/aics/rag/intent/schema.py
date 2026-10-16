"""LLM output schema of condense_classify (意图识别 §5.2.1)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Intent = Literal["kb_qa", "chitchat", "human", "out_of_scope", "action", "clarify", "recall"]
SLOT_KEYS: tuple[str, ...] = ("order_no", "product", "phone")  # v1 fixed (§5.2.3)

SlotKey = Annotated[str, StringConstraints(pattern=r"^[a-z_]{1,32}$")]
SlotValue = Annotated[str, StringConstraints(max_length=100)]


class IntentResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    rewritten_query: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
    ]
    intent: Intent
    confidence: float = Field(ge=0, le=1)
    need_clarify: bool = False
    clarify_question: Annotated[str, StringConstraints(max_length=100)] | None = None
    slots: dict[SlotKey, SlotValue | None] = Field(default_factory=dict, max_length=8)
