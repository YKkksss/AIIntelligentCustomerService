"""Chitchat categories, built-in words and default replies (意图识别 §5.1)."""

from __future__ import annotations

from aics.rag.config import CHITCHAT_BUILTIN, IntentConfig
from aics.rag.intent.normalize import normalize, strip_particles

CATEGORIES: tuple[str, ...] = ("greeting", "thanks", "ack", "bye")

BUILTIN_WORDS = CHITCHAT_BUILTIN

DEFAULT_REPLIES: dict[str, str] = {
    "greeting": "您好，请问有什么可以帮您？",
    "thanks": "不客气，还有其他问题随时问我。",
    "ack": "好的，还有什么可以帮您？",
    "bye": "感谢您的咨询，再见！",
}

_WORD_CATEGORY: dict[str, str] = {
    normalize(w): cat for cat, words in BUILTIN_WORDS.items() for w in words
}


def chitchat_category(text: str) -> str:
    """Category of a (possibly un-normalised) chitchat text; unknown words → greeting."""
    n = normalize(text)
    return _WORD_CATEGORY.get(n) or _WORD_CATEGORY.get(strip_particles(n)) or "greeting"


def chitchat_reply(category: str, cfg: IntentConfig, *, greeting: str | None = None) -> str:
    """App override (`intent.chitchat_replies`) → app greeting (greeting only) → default."""
    if cfg.chitchat_replies is not None:
        text = getattr(cfg.chitchat_replies, category, "") or ""
        if text.strip():
            return text
    if category == "greeting" and greeting and greeting.strip():
        return greeting
    return DEFAULT_REPLIES.get(category, DEFAULT_REPLIES["greeting"])
