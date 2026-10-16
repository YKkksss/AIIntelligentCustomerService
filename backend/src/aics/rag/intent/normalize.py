"""Text normalisation for the rule layer (意图识别 §5.1).

strip → NFKC (full-width → half-width) → lower-case → drop punctuation, symbols, emoji and
whitespace. Repeated characters are kept ("嗯嗯" is a keyword of its own); repeated
punctuation such as "！！" / "~~" disappears with the punctuation itself.
"""

from __future__ import annotations

import unicodedata

# Trailing modal particles tolerated by chitchat exact matching ("你好呀" → "你好").
MODAL_PARTICLES = "呀啊啦哈哦嘛呢吧哟噢喔哇"


def _keep(ch: str) -> bool:
    cat = unicodedata.category(ch)
    # Letters (L*) and numbers (N*) stay; punctuation (P*), symbols / emoji (S*),
    # separators (Z*) and control / format characters (C*) are dropped.
    return cat[0] in ("L", "N")


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text.strip()).lower()
    return "".join(ch for ch in t if _keep(ch))


def strip_particles(text: str) -> str:
    """Remove trailing modal particles, keeping at least one character."""
    out = text
    while len(out) > 1 and out[-1] in MODAL_PARTICLES:
        out = out[:-1]
    return out
