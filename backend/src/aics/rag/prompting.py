"""Prompt rendering helpers (RAG 流程设计 §5, 对话上下文管理 §6.4).

Templates contain literal JSON examples, so `str.format` cannot be used; only the
placeholders a prompt declares are substituted and everything else is left verbatim.
Values are inserted once (no recursive expansion), so user text containing `{question}`
cannot inject further substitutions.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from aics.core.prompts import Prompt, get_prompt
from aics.rag.ports import ChatTurn, RecalledSnippet
from aics.rag.retrieval import Candidate

PROMPT_FILES: dict[str, str] = {
    "condense_classify": "rag/prompts/condense_classify.md",
    "condense": "rag/prompts/condense.md",
    "generate": "rag/prompts/generate.md",
    "judge": "rag/prompts/judge.md",
}
# Fixed order + abbreviation for the `prompt_version` string (e.g. "cc1.gen1.judge1").
PROMPT_ABBR: tuple[tuple[str, str], ...] = (
    ("condense_classify", "cc"),
    ("condense", "cd"),
    ("judge", "judge"),
    ("generate", "gen"),
)

ANSWER_STYLES: dict[str, str] = {
    "concise": "简洁，先给结论，必要时补充关键条件",
    "detailed": "详细，分点说明条件、步骤与注意事项",
    "custom": "遵循上面的应用说明",
}
LANGUAGES: dict[str, str] = {"zh-CN": "简体中文", "zh-TW": "繁體中文", "en": "English"}

ROLE_LABEL: dict[str, str] = {"user": "user", "assistant": "assistant", "human_agent": "agent"}
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


def load(name: str) -> Prompt:
    return get_prompt(PROMPT_FILES[name])


def render(prompt: Prompt, **values: str) -> str:
    """Substitute `{name}` placeholders listed in `values`; unknown braces stay as-is."""

    def sub(m: re.Match[str]) -> str:
        key = m.group(1)
        return values[key] if key in values else m.group(0)

    return _PLACEHOLDER.sub(sub, prompt.body)


def prompt_version_string(versions: Mapping[str, str]) -> str:
    return ".".join(f"{abbr}{versions[name]}" for name, abbr in PROMPT_ABBR if name in versions)


# ---------------------------------------------------------------- conversation data
def _clean(text: str) -> str:
    # Collapse newlines so a turn cannot fake a new tag line / role marker.
    return " ".join(text.split())


def format_history(turns: Sequence[ChatTurn]) -> str:
    return " ".join(f"[{ROLE_LABEL[t.role]}] {_clean(t.content)}" for t in turns)


def format_facts(facts: Mapping[str, str]) -> str:
    return "; ".join(f"{k}={_clean(v)}" for k, v in facts.items())


def format_summary(summary: Mapping[str, Any] | None) -> str:
    if not summary:
        return ""
    return json.dumps(summary, ensure_ascii=False, separators=(",", ":"))


def format_recall(snippets: Iterable[RecalledSnippet]) -> str:
    return " ".join(f"[{s.role}] {_clean(s.content)}" for s in snippets)


# ---------------------------------------------------------------- context block
def format_context(candidates: Sequence[Candidate], *, max_chars: int | None = None) -> str:
    """Numbered sources: `[n] 《title》heading` + text (RAG 流程设计 §5 generate)."""
    parts: list[str] = []
    for i, c in enumerate(candidates, start=1):
        head = f"[{i}] 《{c.chunk.document_title}》"
        if c.heading_path:
            head += c.heading_path
        text = c.text if max_chars is None else c.text[:max_chars]
        parts.append(f"{head}\n{text}")
    return "\n\n".join(parts)


def app_scope(description: str | None, system_prompt: str | None) -> str:
    """`{app_scope}` (意图识别 §5.2.3)."""
    if description and description.strip():
        return _clean(description)
    if system_prompt and system_prompt.strip():
        return _clean(system_prompt)[:200]
    return "未说明，除明显无关的请求外按 kb_qa 处理"
