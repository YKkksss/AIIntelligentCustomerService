"""condense_classify output → final intent (意图识别 §5.2.2), pure functions.

The table rows are applied in order; the first row that rewrites the intent sets
`fallback_reason` and `source="fallback"`. A result that passes untouched is `source="llm"`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from aics.rag.config import HandoffConfig, IntentConfig
from aics.rag.intent.schema import SLOT_KEYS, IntentResult

_JSON_OBJECT = re.compile(r"\{.*\}", re.S)


@dataclass
class ResolvedIntent:
    intent: str  # kb_qa / chitchat / human / out_of_scope / action / clarify
    rewritten_query: str
    confidence: float
    source: str  # llm / fallback
    fallback_reason: str | None = None
    raw_intent: str | None = None
    raw_confidence: float | None = None
    need_clarify: bool = False
    clarify_question: str | None = None
    slots: dict[str, str | None] = field(default_factory=dict)
    rule_hit: dict[str, str | None] | None = None
    mode: str = "merged"  # merged / parallel / rule
    scope_check: dict[str, Any] | None = None  # out-of-scope safeguard details

    def trace(self) -> dict[str, Any]:
        return {
            "name": self.intent,
            "confidence": self.confidence,
            "source": self.source,
            "raw_intent": self.raw_intent,
            "raw_confidence": self.raw_confidence,
            "fallback_reason": self.fallback_reason,
            "need_clarify": self.need_clarify,
            "slots": dict(self.slots),
            "rule_hit": self.rule_hit,
            "mode": self.mode,
            "scope_check": self.scope_check,
        }


def normalize_length(text: str) -> int:
    """Length used for `chitchat_max_chars`: characters without whitespace."""
    return len("".join(text.split()))


def from_rule(question: str, intent: str, rule_hit: dict[str, str | None]) -> ResolvedIntent:
    """Rule-layer verdict: source=rule, confidence 1.0 (§5.1)."""
    return ResolvedIntent(
        intent=intent,
        rewritten_query=question,
        confidence=1.0,
        source="rule",
        rule_hit=rule_hit,
        mode="rule",
    )


def fallback(question: str, reason: str, *, rewritten: str | None = None) -> ResolvedIntent:
    return ResolvedIntent(
        intent="kb_qa",
        rewritten_query=rewritten or question,
        confidence=0.0,
        source="fallback",
        fallback_reason=reason,
    )


def parse_intent_json(text: str) -> tuple[IntentResult | None, str | None]:
    """Returns (validated result, salvageable rewritten_query)."""
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`").removeprefix("json").strip()
    data: Any = None
    try:
        data = json.loads(raw)
    except ValueError:
        m = _JSON_OBJECT.search(raw)
        if m:
            try:
                data = json.loads(m.group(0))
            except ValueError:
                data = None
    if not isinstance(data, dict):
        return None, None
    salvaged = data.get("rewritten_query")
    salvaged = salvaged.strip()[:500] if isinstance(salvaged, str) and salvaged.strip() else None
    try:
        return IntentResult.model_validate(data), salvaged
    except ValidationError:
        return None, salvaged


def resolve(
    result: IntentResult,
    *,
    question: str,
    cfg: IntentConfig,
    handoff: HandoffConfig,
    last_assistant_was_clarify: bool,
) -> ResolvedIntent:
    slots = {k: v for k, v in result.slots.items() if k in SLOT_KEYS}
    out = ResolvedIntent(
        intent=result.intent,
        rewritten_query=result.rewritten_query,
        confidence=result.confidence,
        source="llm",
        raw_intent=result.intent,
        raw_confidence=result.confidence,
        need_clarify=result.need_clarify,
        clarify_question=(result.clarify_question or "").strip() or None,
        slots=slots,
    )

    def to_kb(reason: str) -> ResolvedIntent:
        out.intent, out.source, out.fallback_reason = "kb_qa", "fallback", reason
        return out

    if result.confidence < cfg.min_confidence:
        return to_kb("low_confidence")
    wants_clarify = result.intent == "clarify" or result.need_clarify
    intent = "clarify" if wants_clarify else result.intent
    if intent not in cfg.enabled_intents:
        if intent == "action" and handoff.enabled:
            out.intent, out.source, out.fallback_reason = "human", "fallback", "disabled_intent"
            return out
        return to_kb("disabled_intent")
    if intent in ("human", "action") and not handoff.enabled:
        return to_kb("handoff_disabled")
    if intent == "chitchat" and normalize_length(question) > cfg.chitchat_max_chars:
        return to_kb("chitchat_too_long")
    if intent == "clarify" and (
        result.confidence < cfg.clarify_threshold
        or not out.clarify_question
        or last_assistant_was_clarify
    ):
        return to_kb("clarify_rejected")
    out.intent = intent
    return out
