"""入库注入检测（安全设计 §3 第 5 条）：切块正文匹配典型注入短语，命中 → 待复核。

规则在 `injection_patterns.yaml`（与切块预设一致：配置文件定义、启动时编译一次）。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

PATTERNS_FILE = Path(__file__).with_name("injection_patterns.yaml")
REASON_MAX_CHARS = 120
_ZERO_WIDTH = re.compile("[\u200b-\u200f\u2060-\u2064\ufeff\u00ad]")
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class InjectionHit:
    rule: str  # 例如 zh.ignore_previous
    matched: str

    @property
    def reason(self) -> str:
        return f"{self.rule}: {self.matched}"[:REASON_MAX_CHARS]


@lru_cache(maxsize=1)
def _rules() -> tuple[tuple[str, re.Pattern[str], bool], ...]:
    raw = yaml.safe_load(PATTERNS_FILE.read_text(encoding="utf-8"))
    out: list[tuple[str, re.Pattern[str], bool]] = []
    for lang, compact in (("zh", True), ("en", False)):
        for name, pat in (raw.get(lang) or {}).items():
            out.append((f"{lang}.{name}", re.compile(pat, re.IGNORECASE | re.MULTILINE), compact))
    return tuple(out)


def _normalize(text: str) -> tuple[str, str]:
    t = _ZERO_WIDTH.sub("", unicodedata.normalize("NFKC", text)).lower()
    spaced = "\n".join(_WS.sub(" ", line).strip() for line in t.splitlines())
    return spaced, _WS.sub("", t)


def detect_injection(text: str) -> InjectionHit | None:
    """返回第一条命中的规则；未命中返回 None。"""
    if not text:
        return None
    spaced, compact = _normalize(text)
    for rule, pat, use_compact in _rules():
        m = pat.search(compact if use_compact else spaced)
        if m:
            return InjectionHit(rule, m.group(0).strip())
    return None
