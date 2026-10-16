"""Rule layer (guard) as pure functions (意图识别 §5.1).

Order (first hit wins): sensitive keyword → handoff keyword → refusal streak → chitchat exact
match. With `intent.enabled=false` only the handoff keyword and refusal-streak rules run
(§5.4). Keyword "contains" matching uses an Aho-Corasick automaton built once per word list
(cached by the normalised word tuple, which changes whenever the app config changes — the
same effect as keying by `(app_id, config_version)` without needing either inside rag).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache

from aics.rag.config import HandoffConfig, IntentConfig
from aics.rag.intent.normalize import normalize, strip_particles
from aics.rag.intent.templates import chitchat_category

CHITCHAT_RULE_MAX_CHARS = 8  # normalised length limit for the exact-match rule (§5.1)
MATCHER_CACHE_SIZE = 256


class Matcher:
    """Aho-Corasick automaton over normalised words; `find` returns the first match."""

    def __init__(self, words: Iterable[str]) -> None:
        self._goto: list[dict[str, int]] = [{}]
        self._fail: list[int] = [0]
        self._out: list[str | None] = [None]
        for w in words:
            if w:
                self._add(w)
        self._build()

    def _add(self, word: str) -> None:
        node = 0
        for ch in word:
            nxt = self._goto[node].get(ch)
            if nxt is None:
                nxt = len(self._goto)
                self._goto[node][ch] = nxt
                self._goto.append({})
                self._fail.append(0)
                self._out.append(None)
            node = nxt
        if self._out[node] is None:
            self._out[node] = word

    def _build(self) -> None:
        queue: deque[int] = deque(self._goto[0].values())
        while queue:
            node = queue.popleft()
            for ch, nxt in self._goto[node].items():
                queue.append(nxt)
                f = self._fail[node]
                while f and ch not in self._goto[f]:
                    f = self._fail[f]
                cand = self._goto[f].get(ch, 0)
                self._fail[nxt] = cand if cand != nxt else 0
                if self._out[nxt] is None:
                    self._out[nxt] = self._out[self._fail[nxt]]

    def find(self, text: str) -> str | None:
        node = 0
        for ch in text:
            while node and ch not in self._goto[node]:
                node = self._fail[node]
            node = self._goto[node].get(ch, 0)
            if self._out[node] is not None:
                return self._out[node]
        return None


@lru_cache(maxsize=MATCHER_CACHE_SIZE)
def _matcher(words: tuple[str, ...]) -> Matcher:
    return Matcher(words)


def build_matcher(words: Iterable[str]) -> Matcher:
    return _matcher(tuple(sorted({normalize(w) for w in words} - {""})))


@dataclass(frozen=True)
class RuleHit:
    rule: str  # sensitive / handoff_keyword / max_refusals / chitchat
    intent: str  # blocked / human / chitchat
    word: str | None = None
    category: str | None = None  # chitchat category

    def trace(self) -> dict[str, str | None]:
        return {"rule": self.rule, "word": self.word, "category": self.category}


def apply_rules(
    text: str,
    cfg: IntentConfig,
    handoff: HandoffConfig,
    refusal_streak: int,
) -> RuleHit | None:
    n = normalize(text)
    if cfg.enabled and cfg.sensitive_keywords:
        w = build_matcher(cfg.sensitive_keywords).find(n)
        if w:
            return RuleHit("sensitive", "blocked", w)
    if handoff.enabled:
        w = build_matcher(handoff.keywords).find(n) if handoff.keywords else None
        if w:
            return RuleHit("handoff_keyword", "human", w)
        if refusal_streak >= handoff.max_refusals:
            return RuleHit("max_refusals", "human")
    if (
        cfg.enabled
        and "chitchat" in cfg.enabled_intents
        and n
        and len(n) <= CHITCHAT_RULE_MAX_CHARS
    ):
        words = {normalize(w) for w in cfg.chitchat_keywords}
        for cand in (n, strip_particles(n)):
            if cand in words:
                return RuleHit("chitchat", "chitchat", cand, chitchat_category(cand))
    return None


def next_refusal_streak(
    prev: int, *, intent: str, answer_type: str, interrupted: bool = False
) -> int:
    """`conversations.refusal_streak` after a turn (意图识别 §6.4), for chat.service:
    kb_qa refused +1, kb_qa answered → 0, any handoff → 0; other intents, interrupted and
    error turns leave it unchanged."""
    if interrupted or answer_type == "error":
        return prev
    if answer_type == "handoff":
        return 0
    if intent != "kb_qa":
        return prev
    return prev + 1 if answer_type == "refused" else 0
