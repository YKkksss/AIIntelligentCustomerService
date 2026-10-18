"""load_context assembly (对话上下文管理 §6.2, §10).

Budget `B = history_token_budget`, filled in priority order:
1. key facts (enable_facts): active facts, importance desc then newest first, capped at
   FACTS_MAX_TOKENS; facts that do not fit are skipped (still stored);
2. rolling summary (if any);
3. recent turns after `summary_until_msg_id`, newest turn first, while they fit;
4. floor: the last `keep_recent_turns` turns are always kept; when they exceed the budget,
   single messages longer than MESSAGE_TRUNCATE_TOKENS are cut and `truncated` is flagged;
5. older turns neither summarised nor fitting are dropped and `history_gap` is flagged
   (also the compaction trigger signal, §7.1).

Rendering (§6.2 note): a refused assistant reply becomes "（已拒答）"; human_agent messages keep
their role (rendered as [人工客服] by the prompt formatter); system messages never appear
(the store already excludes them).

This module is pure with respect to I/O except through `ConversationStore`; it does not
import aics.rag (lint-imports) — `aics.modules.chat.memory_port` adapts it to rag's MemoryPort.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from aics.core.tokens import count_tokens, truncate_tokens
from aics.memory.schemas import Fact, StoredMessage, Turn
from aics.memory.store import ConversationStore

FACTS_MAX_TOKENS = 300  # §6.2 / 设计讨论 0002 C2
MESSAGE_TRUNCATE_TOKENS = 500  # §6.2 step 4
REFUSED_PLACEHOLDER = "（已拒答）"

Role = Literal["user", "assistant", "human_agent"]


@dataclass(frozen=True, slots=True)
class HistoryMessage:
    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class ContextQuery:
    """Mirror of rag.ports.ContextRequest (memory may not import rag)."""

    app_id: UUID
    conversation_id: UUID | None
    question: str
    budget: int
    keep_recent_turns: int
    enable_facts: bool = True
    inline_history: Sequence[HistoryMessage] | None = None
    temp_summary: bool = True
    # The current turn's user message when chat.service persisted it before generation.
    exclude_message_id: UUID | None = None


@dataclass(slots=True)
class LoadedContext:
    history: list[HistoryMessage] = field(default_factory=list)
    facts: dict[str, str] = field(default_factory=dict)
    summary: dict[str, Any] | None = None
    summary_version: int = 0
    recent_turns: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)


# Stateless temp summary hook (§10): (overflow messages, app_id) -> summary dict or None.
TempSummarizer = Callable[[list[HistoryMessage]], Awaitable[dict[str, Any] | None]]


# ---------------------------------------------------------------- rendering
def render_fact_line(key: str, value: str) -> str:
    return f"{key}={' '.join(value.split())}"


def select_facts(facts: Sequence[Fact], max_tokens: int = FACTS_MAX_TOKENS) -> dict[str, str]:
    """Active facts in injection order that fit in `max_tokens` (separator counted)."""
    out: dict[str, str] = {}
    used = 0
    for f in facts:
        if not f.active:
            continue
        cost = count_tokens(render_fact_line(f.key, f.value)) + 1
        if used + cost > max_tokens:
            continue
        out[f.key] = f.value
        used += cost
    return out


def facts_tokens(facts: dict[str, str]) -> int:
    return count_tokens("; ".join(render_fact_line(k, v) for k, v in facts.items()))


def summary_tokens(summary: dict[str, Any] | None) -> int:
    if not summary:
        return 0
    return count_tokens(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))


def render_message(m: StoredMessage) -> HistoryMessage | None:
    if m.role == "system":
        return None
    role: Role = m.role if m.role in ("user", "assistant", "human_agent") else "assistant"  # type: ignore[assignment]
    if m.role == "assistant" and m.answer_type == "refused":
        return HistoryMessage(role, REFUSED_PLACEHOLDER)
    return HistoryMessage(role, m.content)


@dataclass(frozen=True, slots=True)
class _RTurn:
    messages: tuple[HistoryMessage, ...]

    @property
    def tokens(self) -> int:
        return sum(count_tokens(m.content) for m in self.messages)


def _render_turn(t: Turn) -> _RTurn:
    return _RTurn(tuple(h for m in t.messages if (h := render_message(m)) is not None))


def _truncate_turn(t: _RTurn) -> tuple[_RTurn, bool]:
    cut = False
    out: list[HistoryMessage] = []
    for m in t.messages:
        if count_tokens(m.content) > MESSAGE_TRUNCATE_TOKENS:
            out.append(HistoryMessage(m.role, truncate_tokens(m.content, MESSAGE_TRUNCATE_TOKENS)))
            cut = True
        else:
            out.append(m)
    return _RTurn(tuple(out)), cut


@dataclass(frozen=True, slots=True)
class Fill:
    taken: list[_RTurn]  # oldest first
    dropped: list[_RTurn]  # oldest first (did not fit, not summarised)
    tokens: int
    truncated: bool


def fill_recent(turns_newest_first: Sequence[_RTurn], remain: int, keep: int) -> Fill:
    """Steps 3–5 of §6.2 on rendered turns."""
    taken: list[_RTurn] = []
    used = 0
    truncated = False
    for t in turns_newest_first:
        tok = t.tokens
        if len(taken) < keep:
            if tok > remain - used:
                t, cut = _truncate_turn(t)
                truncated = truncated or cut
                tok = t.tokens
        elif tok > remain - used:
            break
        taken.append(t)
        used += tok
    # Turns are taken as a contiguous newest-first prefix; the rest is dropped.
    dropped = list(turns_newest_first[len(taken) :])
    return Fill(list(reversed(taken)), list(reversed(dropped)), used, truncated)


def group_inline(messages: Sequence[HistoryMessage]) -> list[_RTurn]:
    """Group caller-provided messages into turns, newest turn first."""
    turns: list[list[HistoryMessage]] = []
    for m in messages:
        if m.role == "user" or not turns:
            turns.append([m])
        else:
            turns[-1].append(m)
    return [_RTurn(tuple(t)) for t in reversed(turns)]


def _flatten(turns: Sequence[_RTurn]) -> list[HistoryMessage]:
    return [m for t in turns for m in t.messages]


# ---------------------------------------------------------------- assembly
class ContextAssembler:
    def __init__(
        self, store: ConversationStore, *, temp_summarizer: TempSummarizer | None = None
    ) -> None:
        self.store = store
        self.temp_summarizer = temp_summarizer

    async def load(self, q: ContextQuery) -> LoadedContext:
        # 无状态请求：没有会话（OpenAI 兼容不带会话头），或历史由调用方随请求提供
        # （带会话头的请求也可以自带历史，此时以请求内历史为准，不读服务端存储）。
        if q.conversation_id is None or q.inline_history is not None:
            return await self._from_inline(q)
        cid = q.conversation_id
        facts: dict[str, str] = {}
        if q.enable_facts:
            facts = select_facts(await self.store.list_facts(q.app_id, cid))
        st = await self.store.get_summary_state(q.app_id, cid)
        summary = st.summary.model_dump() if st.summary is not None else None
        used_f, used_s = facts_tokens(facts), summary_tokens(summary)
        turns = await self.store.turns_after(q.app_id, cid, st.until_msg_id)
        turns = _drop_current(turns, q)
        rendered = [r for t in turns if (r := _render_turn(t)).messages]
        fill = fill_recent(rendered, q.budget - used_f - used_s, q.keep_recent_turns)
        flags: list[str] = []
        if fill.truncated:
            flags.append("truncated")
        if fill.dropped:
            flags.append("history_gap")
        return LoadedContext(
            history=_flatten(fill.taken),
            facts=facts,
            summary=summary,
            summary_version=st.version,
            recent_turns=len(fill.taken),
            usage={
                "facts": used_f,
                "summary": used_s,
                "recent": fill.tokens,
                "recall": 0,
                "budget": q.budget,
            },
            flags=flags,
        )

    async def _from_inline(self, q: ContextQuery) -> LoadedContext:
        """Stateless request (§10): same trimming on the caller's history; optional
        in-request temp summary of what does not fit (not persisted)."""
        rendered = group_inline(list(q.inline_history or []))
        fill = fill_recent(rendered, q.budget, q.keep_recent_turns)
        flags: list[str] = ["truncated"] if fill.truncated else []
        summary: dict[str, Any] | None = None
        if fill.dropped and q.temp_summary and self.temp_summarizer is not None:
            summary = await self.temp_summarizer(_flatten(fill.dropped))
            if summary is not None:
                flags.append("temp_summary")
                # The temp summary must also fit: drop oldest kept turns beyond the floor.
                fill = fill_recent(
                    list(reversed(fill.taken)),
                    q.budget - summary_tokens(summary),
                    q.keep_recent_turns,
                )
        if fill.dropped:
            flags.append("history_gap")
        return LoadedContext(
            history=_flatten(fill.taken),
            summary=summary,
            recent_turns=len(fill.taken),
            usage={
                "facts": 0,
                "summary": summary_tokens(summary),
                "recent": fill.tokens,
                "recall": 0,
                "budget": q.budget,
            },
            flags=flags,
        )


def _drop_current(turns: list[Turn], q: ContextQuery) -> list[Turn]:
    """Remove the current question's user message if chat.service already persisted it."""
    if not turns:
        return turns
    newest = turns[0]
    if q.exclude_message_id is not None:
        if newest.messages[0].id == q.exclude_message_id:
            rest = newest.messages[1:]
            return ([Turn(rest)] if rest else []) + turns[1:]
        return turns
    only = newest.messages
    if len(only) == 1 and only[0].role == "user" and only[0].content.strip() == q.question.strip():
        return turns[1:]
    return turns
