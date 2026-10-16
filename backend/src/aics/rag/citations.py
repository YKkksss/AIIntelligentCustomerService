"""Citation markers and output checks for postprocess (RAG 流程设计 §4, 安全设计 §3.4).

Markers: `[n]`, adjacent `[1][2]`, and lists inside one bracket (`[1,2]`, `[1、2]`,
`[1, 2]`); full-width brackets `［1］` / `【1】` are accepted too. Numbers outside
`1..n_sources` are removed from the text. Citations are returned in order of first use and
keep the number the model wrote (the client maps `[n]` → `citations[*].index`).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

_MARKER = re.compile(r"[\[［【]\s*(\d{1,3}(?:\s*[,，、]\s*\d{1,3})*)\s*[\]］】]")
_SPLIT = re.compile(r"[,，、]")
_SPACES_BEFORE_PUNCT = re.compile(r"[ \t]+([，。！？；：,.!?;:])")

# "作为 AI 我被要求……" style leaks of the instructions (安全设计 §3.4)
_LEAK_PATTERNS = [
    re.compile(p)
    for p in (
        r"作为(一个)?\s*(AI|人工智能|语言模型|客服助手)[^。！？\n]{0,12}(被要求|被设定|被指示|的(系统)?提示词)",
        r"我的(系统)?(提示词|指令)(是|如下|包括)",
        r"(system\s*prompt|系统提示词)\s*[:：]",
        r"(优先级最高，任何资料或用户内容都不能改变)",
        r"<\s*(context|conversation_background)\s*>",
    )
]
LEAK_NGRAM = 20  # a verbatim run this long from the system prompt counts as a leak


@dataclass
class ParsedCitations:
    text: str  # answer with out-of-range markers removed
    used: list[int]  # 1-based source numbers in order of first appearance
    dropped: list[int]  # out-of-range numbers that were removed


def parse_citations(answer: str, n_sources: int) -> ParsedCitations:
    used: list[int] = []
    dropped: list[int] = []

    def sub(m: re.Match[str]) -> str:
        keep: list[int] = []
        for part in _SPLIT.split(m.group(1)):
            n = int(part.strip())
            if 1 <= n <= n_sources:
                keep.append(n)
                if n not in used:
                    used.append(n)
            else:
                dropped.append(n)
        return "".join(f"[{n}]" for n in dict.fromkeys(keep))

    text = _MARKER.sub(sub, answer)
    if dropped:
        text = _SPACES_BEFORE_PUNCT.sub(r"\1", text)
    return ParsedCitations(text=text, used=used, dropped=dropped)


def leaks_instructions(answer: str, protected: Sequence[str]) -> bool:
    """True when the answer looks like it reveals the system prompt / rules."""
    if any(p.search(answer) for p in _LEAK_PATTERNS):
        return True
    compact = "".join(answer.split())
    for src in protected:
        s = "".join(src.split())
        if len(s) < LEAK_NGRAM:
            continue
        for i in range(0, len(s) - LEAK_NGRAM + 1, LEAK_NGRAM // 2):
            if s[i : i + LEAK_NGRAM] in compact:
                return True
    return False
