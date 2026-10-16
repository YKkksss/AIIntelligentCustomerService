"""Rolling summary + fact extraction (对话上下文管理 §7).

`Compactor.run` (one conversation):
1. take the Redis / local lock (none → `skipped_lock`); stop if the app is deleting;
2. read summary state; overflow = turns after `summary_until_msg_id` except the most recent
   `keep_recent_turns` (oldest first); nothing → `nothing_to_do`;
3. per batch (≤ COMPACT_BATCH_TOKENS, oldest first, at least one turn): summary call
   (old summary + batch) and, with enable_facts, fact extraction; each output is validated
   with Pydantic and retried once with the error; still invalid → `failed`, nothing written;
4. one transaction per batch: `save_summary(expected_version)` (lost race →
   `version_conflict`, rolled back, facts not written) + fact reconciliation;
5. release the lock.

Upstream errors (network / timeout / 429 / 5xx) are re-raised as `CompactionUpstreamError`
after the attempt is recorded, so the Celery task can autoretry; messages, the old summary
and facts are untouched by a failed batch (each batch commits atomically).
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

import structlog
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from aics.core import metrics
from aics.core.prompts import get_prompt, render_prompt
from aics.core.tokens import count_tokens
from aics.memory.context import HistoryMessage, render_message
from aics.memory.facts import FactCandidate, FactOutcome, reconcile
from aics.memory.locks import CompactLock
from aics.memory.schemas import FACT_KEY_PATTERN, ConversationSummary, Fact, Turn
from aics.memory.units import StoreFactory
from aics.providers.llm import is_upstream_failure

log = structlog.get_logger()

COMPACT_BATCH_TOKENS = 8000  # §7.2 single-batch cap
SUMMARY_MAX_TOKENS = 800  # {max_summary_tokens} in the summary prompt
SUMMARY_CALL_MAX_TOKENS = 1200
EXTRACT_CALL_MAX_TOKENS = 600
COMPACT_TIMEOUT_S = 60.0
TEMP_SUMMARY_TIMEOUT_S = 15.0
SUMMARY_PROMPT = "memory/prompts/compact_summary.md"
EXTRACT_PROMPT = "memory/prompts/extract_facts.md"

Result = Literal[
    "success", "nothing_to_do", "skipped_lock", "version_conflict", "failed", "app_deleting"
]


class CompactionUpstreamError(Exception):
    """LLM upstream outage during compaction (Celery autoretry)."""


class CompactParams(BaseModel):
    """Everything compaction needs from the app config (memory never reads apps, §0)."""

    model_config = ConfigDict(frozen=True)

    keep_recent_turns: int = Field(4, ge=1, le=20)
    compact_trigger_tokens: int = Field(6000, ge=0)
    enable_facts: bool = True
    model: str | None = None  # summary_model or llm_model; None = provider default


class ExtractedFact(BaseModel):
    """§7.5."""

    model_config = ConfigDict(extra="ignore")

    key: str = Field(pattern=FACT_KEY_PATTERN)
    value: str | None = Field(None, min_length=1, max_length=200)
    retract: bool = False
    source_index: int
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _value_required(self) -> ExtractedFact:
        if not self.retract and self.value is None:
            raise ValueError("value is required unless retract=true")
        return self


class ExtractedFacts(BaseModel):
    model_config = ConfigDict(extra="ignore")

    facts: list[ExtractedFact] = Field(default_factory=list, max_length=30)


@dataclass
class CompactResult:
    result: Result
    version: int = 0
    batches: int = 0
    until_msg_id: UUID | None = None
    facts: list[FactOutcome] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=lambda: {"prompt": 0, "completion": 0})
    error: str | None = None


# ---------------------------------------------------------------- helpers
def overflow_turns(turns_newest_first: Sequence[Turn], keep: int) -> list[Turn]:
    """Turns not protected by the keep_recent_turns floor, oldest first."""
    return list(reversed(turns_newest_first[keep:]))


def uncovered_tokens(turns_newest_first: Sequence[Turn], keep: int) -> int:
    """§7.1: tokens after summary_until_msg_id and outside the last `keep` turns."""
    return sum(t.token_count for t in turns_newest_first[keep:])


def next_batch(overflow: Sequence[Turn], cap: int = COMPACT_BATCH_TOKENS) -> list[Turn]:
    batch: list[Turn] = []
    used = 0
    for t in overflow:
        if batch and used + t.token_count > cap:
            break
        batch.append(t)
        used += t.token_count
    return batch


def _one_line(text: str) -> str:
    return " ".join(text.split())


def render_turns(messages: Sequence[HistoryMessage], *, numbered: bool) -> str:
    label = {"user": "user", "assistant": "assistant", "human_agent": "agent"}
    lines = []
    for i, m in enumerate(messages):
        prefix = f"[{i}]" if numbered else ""
        lines.append(f"{prefix}[{label[m.role]}] {_one_line(m.content)}")
    return "\n".join(lines)


def _parse_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if 0 <= start < end:
            return json.loads(raw[start : end + 1])
        raise


def facts_json(facts: Sequence[Fact]) -> str:
    return json.dumps(
        [{"key": f.key, "value": f.value} for f in facts if f.active],
        ensure_ascii=False,
        separators=(",", ":"),
    )


class _InvalidOutput(Exception):
    pass


# ---------------------------------------------------------------- compactor
class Compactor:
    def __init__(self, units: StoreFactory, llm: Any, lock: CompactLock) -> None:
        self.units = units
        self.llm = llm  # providers.llm.OpenAICompatibleLLM (chat_model(**overrides))
        self.lock = lock

    # ---- LLM ----
    async def _call(
        self,
        prompt: str,
        *,
        model: str | None,
        max_tokens: int,
        timeout_s: float,
        usage: dict[str, int],
    ) -> str:
        overrides: dict[str, Any] = {
            "temperature": 0.0,
            "max_tokens": max_tokens,
            "timeout": timeout_s,
            "thinking": False,
        }
        if model:
            overrides["model"] = model
        chat = self.llm.chat_model(**overrides)
        try:
            msg = await chat.ainvoke(
                [HumanMessage(content=prompt)], response_format={"type": "json_object"}
            )
        except Exception as e:
            if is_upstream_failure(e) or isinstance(e, TimeoutError):
                raise CompactionUpstreamError(type(e).__name__) from e
            raise
        meta = getattr(msg, "usage_metadata", None) or {}
        usage["prompt"] += int(meta.get("input_tokens") or 0)
        usage["completion"] += int(meta.get("output_tokens") or 0)
        content = msg.content
        return content if isinstance(content, str) else ""

    async def _validated(
        self,
        prompt: str,
        model_cls: type[BaseModel],
        *,
        model: str | None,
        max_tokens: int,
        timeout_s: float,
        usage: dict[str, int],
    ) -> Any:
        """§7.5: validate; on failure retry once with the error appended; then give up."""
        err = ""
        for attempt in range(2):
            text = (
                prompt
                if not err
                else f"{prompt}\n\n上一次输出不合格：{err}\n请严格按格式重新输出 JSON。"
            )
            raw = await self._call(
                text, model=model, max_tokens=max_tokens, timeout_s=timeout_s, usage=usage
            )
            try:
                return model_cls.model_validate(_parse_json(raw))
            except (ValueError, ValidationError) as e:
                err = str(e).splitlines()[0][:300]
                log.warning(
                    "memory.compact_invalid_output", schema=model_cls.__name__, attempt=attempt
                )
        raise _InvalidOutput(err)

    async def summarize(
        self,
        old: ConversationSummary | None,
        messages: Sequence[HistoryMessage],
        *,
        model: str | None,
        usage: dict[str, int],
        timeout_s: float = COMPACT_TIMEOUT_S,
    ) -> ConversationSummary:
        prompt = render_prompt(
            get_prompt(SUMMARY_PROMPT),
            max_summary_tokens=str(SUMMARY_MAX_TOKENS),
            old_summary_json=json.dumps(old.model_dump() if old else {}, ensure_ascii=False),
            new_turns=render_turns(messages, numbered=False),
        )
        out: ConversationSummary = await self._validated(
            prompt,
            ConversationSummary,
            model=model,
            max_tokens=SUMMARY_CALL_MAX_TOKENS,
            timeout_s=timeout_s,
            usage=usage,
        )
        return out

    async def extract(
        self,
        existing: Sequence[Fact],
        messages: Sequence[HistoryMessage],
        *,
        model: str | None,
        usage: dict[str, int],
    ) -> ExtractedFacts:
        prompt = render_prompt(
            get_prompt(EXTRACT_PROMPT),
            facts_json=facts_json(existing),
            turns=render_turns(messages, numbered=True),
        )
        out: ExtractedFacts = await self._validated(
            prompt,
            ExtractedFacts,
            model=model,
            max_tokens=EXTRACT_CALL_MAX_TOKENS,
            timeout_s=COMPACT_TIMEOUT_S,
            usage=usage,
        )
        return out

    async def temp_summary(
        self, messages: list[HistoryMessage], *, model: str | None = None
    ) -> dict[str, Any] | None:
        """Stateless in-request summary (§10): never raises, never persisted."""
        try:
            s = await self.summarize(
                None,
                messages,
                model=model,
                usage={"prompt": 0, "completion": 0},
                timeout_s=TEMP_SUMMARY_TIMEOUT_S,
            )
            return s.model_dump()
        except Exception as e:
            log.warning("memory.temp_summary_failed", error_type=type(e).__name__)
            return None

    # ---- run ----
    async def run(
        self,
        app_id: UUID,
        conversation_id: UUID,
        params: CompactParams,
        *,
        expected_version: int | None = None,
    ) -> CompactResult:
        t0 = time.perf_counter()
        token = await self.lock.acquire(conversation_id)
        if token is None:
            res = CompactResult("skipped_lock")
        else:
            try:
                res = await self._run_locked(app_id, conversation_id, params, expected_version)
            except CompactionUpstreamError as e:
                metrics.inc("aics_compaction_total", app_id=str(app_id), result="failed")
                log.warning("memory.compact_upstream_error", error_type=str(e))
                raise
            finally:
                await self.lock.release(conversation_id, token)
        metrics.inc("aics_compaction_total", app_id=str(app_id), result=res.result)
        metrics.observe("aics_compaction_duration_seconds", time.perf_counter() - t0)
        if res.result == "failed":
            metrics.inc("aics_compaction_failures_total")
        log.info(
            "memory.compact",
            result=res.result,
            version=res.version,
            batches=res.batches,
            prompt_tokens=res.usage["prompt"],
            completion_tokens=res.usage["completion"],
        )
        return res

    async def _run_locked(
        self,
        app_id: UUID,
        cid: UUID,
        params: CompactParams,
        expected_version: int | None,
    ) -> CompactResult:
        res = CompactResult("nothing_to_do")
        first = True
        while True:
            async with self.units() as u:
                if first and await u.store.is_app_deleting(app_id):
                    return CompactResult("app_deleting")
                st = await u.store.get_summary_state(app_id, cid)
                turns = await u.store.turns_after(app_id, cid, st.until_msg_id)
                existing = await u.store.list_facts(app_id, cid) if params.enable_facts else []
            res.version = st.version
            if first and expected_version is not None and st.version != expected_version:
                res.result = "version_conflict"
                return res
            first = False
            batch = next_batch(overflow_turns(turns, params.keep_recent_turns))
            if not batch:
                return res
            stored = [m for t in batch for m in t.messages]
            rendered = [h for m in stored if (h := render_message(m)) is not None]
            try:
                summary = await self.summarize(
                    st.summary, rendered, model=params.model, usage=res.usage
                )
                cands: list[FactCandidate] = []
                if params.enable_facts:
                    extracted = await self.extract(
                        existing, rendered, model=params.model, usage=res.usage
                    )
                    ids = [m.id for m in stored if m.role != "system"]
                    for f in extracted.facts:
                        mid = ids[f.source_index] if 0 <= f.source_index < len(ids) else None
                        cands.append(
                            FactCandidate(
                                key=f.key,
                                value=f.value,
                                confidence=f.confidence,
                                source="compaction",
                                source_message_id=mid,
                                retract=f.retract,
                            )
                        )
            except _InvalidOutput as e:
                res.result, res.error = "failed", str(e)
                return res
            async with self.units() as u:
                ok = await u.store.save_summary(
                    app_id, cid, summary, batch[-1].last_id, expected_version=st.version
                )
                if not ok:
                    await u.rollback()
                    res.result = "version_conflict"
                    return res
                outcomes = await reconcile(u.store, app_id, cid, cands) if cands else []
                await u.commit()
            res.result = "success"
            res.version = st.version + 1
            res.batches += 1
            res.until_msg_id = batch[-1].last_id
            res.facts.extend(outcomes)


def summary_tokens_of(s: ConversationSummary | None) -> int:
    return 0 if s is None else count_tokens(json.dumps(s.model_dump(), ensure_ascii=False))
