"""Back-office debug conversations (M4.4 ②, ADR-0019).

The debug page may pin a temporary `rag_config` patch (`TurnRequest.debug_config`): it is
deep-merged into the app config for this turn only and validated with the same pydantic model
the app config uses, so a bad field is a 422 with its path and nothing is persisted.

Read-only members (a `viewer` role or any role without `app.write`) get the retrieval trace
with phone numbers masked (对话上下文管理 §13) — the panel is useful without leaking PII.

The SSE frames themselves are encoded by `modules.chat.sse` (ISSUE-82: one encoding for the
open API and the back office).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ValidationError
from sqlalchemy import func

from aics.core.errors import ValidationAppError
from aics.core.masking import mask_text
from aics.db.models.job import Job
from aics.memory.compactor import CompactParams
from aics.modules.apps.service import AppService
from aics.modules.chat.repository import ConversationRepository, MessageRepository
from aics.rag.config import AppRagConfig

DEBUG_CHANNEL = "debug"
HISTORY_LIMIT = 50
COMPACT_TYPE = "compact"


# ------------------------------------------------------------------ temporary config
def _merge(base: dict[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in patch.items():
        current = out.get(key)
        if isinstance(value, Mapping) and isinstance(current, Mapping):
            out[key] = _merge(dict(current), value)
        else:
            out[key] = value
    return out


def _unknown_paths(
    model: type[BaseModel], patch: Mapping[str, Any], prefix: list[str]
) -> list[list[str]]:
    """`AppRagConfig` 是 `extra="ignore"`：拼错的键会被静默丢掉，调试页必须报错。"""
    out: list[list[str]] = []
    for key, value in patch.items():
        field = model.model_fields.get(key)
        if field is None:
            out.append([*prefix, key])
            continue
        nested = field.annotation
        if (
            isinstance(nested, type)
            and issubclass(nested, BaseModel)
            and isinstance(value, Mapping)
        ):
            out.extend(_unknown_paths(nested, value, [*prefix, key]))
    return out


def merge_debug_config(base: AppRagConfig, patch: Mapping[str, Any]) -> AppRagConfig:
    """调试页的临时参数：深合并后按应用配置同一套规则校验（422 带字段路径）。"""
    if not isinstance(patch, Mapping):
        raise ValidationAppError(
            details={"errors": [{"loc": ["debug_config"], "msg": "必须是对象"}]}
        )
    unknown = _unknown_paths(AppRagConfig, patch, [])
    if unknown:
        raise ValidationAppError(
            message="调试参数校验失败",
            details={
                "errors": [{"loc": ["debug_config", *path], "msg": "未知字段"} for path in unknown]
            },
        )
    merged = _merge(base.model_dump(mode="json"), patch)
    try:
        return AppRagConfig.model_validate(merged)
    except ValidationError as e:
        raise ValidationAppError(
            message="调试参数校验失败",
            details={
                "errors": [
                    {"loc": ["debug_config", *err["loc"]], "msg": err["msg"]} for err in e.errors()
                ]
            },
        ) from e


# ------------------------------------------------------------------ PII masking
def _masked(value: Any) -> Any:
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, Mapping):
        return {str(k): _masked(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_masked(v) for v in value]
    return value


def masked_trace(trace: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Retrieval trace for a read-only member: phone numbers inside strings are masked."""
    if trace is None:
        return None
    masked = _masked(trace)
    return masked if isinstance(masked, dict) else None


def message_dict(m: Any, *, mask: bool) -> dict[str, Any]:
    trace = m.retrieval_trace
    return {
        "id": m.id,
        "role": m.role,
        "kind": m.kind,
        "content": mask_text(m.content) if mask else m.content,
        "answer_type": m.answer_type,
        "handoff": m.handoff,
        "intent": (
            {"name": m.intent, "confidence": m.intent_confidence, "source": m.intent_source}
            if m.intent
            else None
        ),
        "citations": _masked(m.citations) if mask else m.citations,
        "trace": masked_trace(trace) if mask else trace,
        "interrupted": m.interrupted,
        "created_at": m.created_at,
    }


# ------------------------------------------------------------------ service
class DebugChat:
    """Read a debug conversation and run a forced compaction (`jobs(type=compact)`)."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    async def history(self, *, app_id: UUID, conversation_id: UUID, mask: bool) -> dict[str, Any]:
        async with self.runtime.sessions() as s:
            conv = await ConversationRepository(s).get_or_404(app_id, conversation_id)
            rows = await MessageRepository(s).list_for_conversation(
                app_id, conversation_id, limit=HISTORY_LIMIT
            )
            items = [message_dict(m, mask=mask) for m in rows]
            conversation = {
                "id": conv.id,
                "channel": conv.channel,
                "status": conv.status,
                "handoff_status": conv.handoff_status,
                "close_reason": conv.close_reason,
                "closed_at": conv.closed_at,
                "created_at": conv.created_at,
                "last_activity_at": conv.last_activity_at,
            }
        return {"conversation": conversation, "items": items}

    async def compact(
        self, *, app_id: UUID, conversation_id: UUID, created_by: UUID | None = None
    ) -> dict[str, Any]:
        async with self.runtime.sessions() as s:
            apps = await AppService.load(s)
            app = await apps.get_app(app_id)
            rag = apps.config_of(app).rag
            await ConversationRepository(s).get_or_404(app_id, conversation_id)
            job = Job(
                app_id=app_id,
                type=COMPACT_TYPE,
                target_id=conversation_id,
                payload={"reason": "admin.debug"},
                created_by=created_by,
                status="running",
            )
            s.add(job)
            await s.commit()
            job_id = job.id
        params = CompactParams(
            keep_recent_turns=rag.keep_recent_turns,
            compact_trigger_tokens=rag.compact_trigger_tokens,
            enable_facts=rag.enable_facts,
            model=rag.summary_model or rag.llm_model,
        )
        try:
            result = await self.runtime.memory.compact_now(app_id, conversation_id, params)
        except Exception as e:
            await self._finish(job_id, status="failed", error=f"{type(e).__name__}: {e}"[:500])
            raise
        await self._finish(job_id, status="done")
        return {
            "job_id": job_id,
            "compacted": bool(result.batches or result.until_msg_id),
            "version": result.version,
            "batches": result.batches,
            "until_msg_id": result.until_msg_id,
            "facts": len(result.facts),
            "usage": result.usage,
        }

    async def _finish(self, job_id: UUID, *, status: str, error: str | None = None) -> None:
        async with self.runtime.sessions() as s:
            job = await s.get(Job, job_id)
            if job is None:  # pragma: no cover - the row was just written
                return
            job.status = status
            job.error = error
            job.finished_at = func.now()
            await s.commit()
