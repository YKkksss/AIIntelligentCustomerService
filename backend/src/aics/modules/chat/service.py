"""chat.service — the single orchestration entry of a conversation turn (模块划分 §4, ADR-0008).

`ChatService.start_turn(req)`:
1. **prepare** (one transaction, before any model call): load the app and its config; resolve
   the conversation (none → new; missing / other app → 404; closed → new conversation in the
   same channel for the same external user, `previous_conversation_id` set, no history carried
   over, 会话生命周期 §2.3); persist the **user message**; dispatch on
   `handoff_status` × `handoff_ai_mode` (§3.2):
   none / released / pending+continue → rag; pending+wait → `system/handoff_wait` reply
   (`reply_by=system`); accepted → no reply (`reply_by=none`).
2. **execute** (its own asyncio task, events through a queue): `run_rag` → stream events →
   persist the assistant message and update the conversation in one transaction
   (`refusal_streak` via `next_refusal_streak`; a handoff turn moves none / released →
   `pending`) → `memory.capture_slots` → `memory.maybe_compact` → `end`.
   Client disconnect cancels the graph; the partial answer is persisted with
   `interrupted=true` (refusal_streak unchanged). Upstream failures persist
   `answer_type=error` (`decision_reason` = error code) and surface as an error event / body.

The turn task is independent of the HTTP response, so persistence after a disconnect is not
subject to the server's cancellation of the response stream.

Stateless turns (M3.3, OpenAI 兼容且不带 `X-AICS-Conversation-Id`): the history travels with the
request (`TurnRequest.inline_history`), so the turn neither writes facts nor compacts; the
conversation created for it is closed right after the turn with `close_reason=stateless`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

import structlog
from sqlalchemy import func

from aics.core import metrics
from aics.core.errors import AppError, ConversationNotFoundError
from aics.db.base import new_id
from aics.memory.compactor import CompactParams
from aics.modules.apps.service import AppConfig, AppService
from aics.modules.chat.repository import ConversationRepository, MessageRepository
from aics.modules.chat.runtime import ChatRuntime
from aics.modules.chat.schemas import NewConversation, NewMessage
from aics.rag.graph import run_rag
from aics.rag.intent.rules import next_refusal_streak
from aics.rag.ports import ChatTurn
from aics.rag.schemas import (
    AppProfile,
    Citation,
    CitationsEvent,
    DeltaEvent,
    RagInput,
    RagResult,
    RunMode,
    TraceEvent,
)

log = structlog.get_logger()

ReplyBy = Literal["ai", "system", "none"]
Dispatch = Literal["rag", "wait", "agent"]
CHANNEL_OF_MODE = {RunMode.online: "api", RunMode.debug: "debug", RunMode.eval: "eval"}


class UpstreamTimeoutError(AppError):
    code, status, message = "UPSTREAM_TIMEOUT", 504, "上游服务超时"


# ---------------------------------------------------------------- requests / events
@dataclass(frozen=True)
class TurnRequest:
    app_id: UUID
    question: str
    conversation_id: UUID | None = None
    external_user_id: str | None = None
    channel: str = "api"
    run_mode: RunMode = RunMode.online
    metadata: dict[str, Any] = field(default_factory=dict)
    api_key_id: UUID | None = None
    created_by: UUID | None = None
    tags: list[str] | None = None
    # 无状态请求（OpenAI 兼容）：历史随请求提供，不读服务端存储，也不做记忆写入
    inline_history: list[ChatTurn] | None = None
    # 请求级覆盖（temperature / max_tokens），只作用于本轮，不改应用配置
    rag_overrides: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StartEvent:
    conversation_id: UUID
    message_id: UUID
    previous_conversation_id: UUID | None


@dataclass(frozen=True)
class TurnResult:
    """Everything the blocking response / SSE `end` needs (开放接口参考 §1)."""

    conversation_id: UUID
    previous_conversation_id: UUID | None
    message_id: UUID
    user_message_id: UUID
    reply_by: ReplyBy
    handoff_status: str
    answer: str | None
    answer_type: str | None
    handoff: bool = False
    handoff_reason: str | None = None
    intent: dict[str, Any] | None = None
    citations: list[dict[str, Any]] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    first_token_ms: int | None = None
    interrupted: bool = False
    degraded: list[str] = field(default_factory=list)

    @property
    def used_citations(self) -> list[int]:
        return [c["n"] for c in self.citations]


@dataclass(frozen=True)
class ErrorEvent:
    code: str
    message: str
    status: int


TurnEvent = StartEvent | CitationsEvent | DeltaEvent | TraceEvent | TurnResult | ErrorEvent


def citation_dict(c: Citation) -> dict[str, Any]:
    """messages.citations / open-API citation object (数据库设计 §3.9)."""
    return {
        "n": c.index,
        "chunk_id": str(c.chunk_id),
        "document_id": str(c.document_id),
        "document_title": c.document_title,
        "heading_path": c.heading_path,
        "snippet": c.snippet,
        "score": None if c.rerank_score is None else round(float(c.rerank_score), 4),
        "page": c.page,
    }


def dispatch_of(handoff_status: str, ai_mode: str) -> Dispatch:
    """会话生命周期与转人工 §3.2."""
    if handoff_status == "accepted":
        return "agent"
    if handoff_status == "pending" and ai_mode == "wait":
        return "wait"
    return "rag"


def profile_of(app: Any) -> AppProfile:
    return AppProfile(
        name=app.name,
        description=app.description,
        greeting=app.greeting,
        system_prompt=app.system_prompt,
        answer_style=app.answer_style,
        fallback_text=app.fallback_text,
        language=app.language,
    )


# ---------------------------------------------------------------- prepared turn
@dataclass
class _Prepared:
    req: TurnRequest
    t0: float
    config: AppConfig
    profile: AppProfile
    custom_dict: list[str]
    config_version: int
    conversation_id: UUID
    previous_conversation_id: UUID | None
    user_message_id: UUID
    reply_id: UUID
    dispatch: Dispatch
    handoff_status: str
    refusal_streak: int
    last_assistant_was_clarify: bool
    system_reply: str | None = None
    stateless: bool = False


class TurnHandle:
    """A running turn: iterate `events()`; call `cancel()` when the client goes away."""

    def __init__(self, service: ChatService, prepared: _Prepared) -> None:
        self.prepared = prepared
        self.queue: asyncio.Queue[TurnEvent | None] = asyncio.Queue()
        self._finishing = False  # generation done → a disconnect no longer interrupts
        self._cancel_requested = False
        self.queue.put_nowait(
            StartEvent(
                prepared.conversation_id,
                prepared.reply_id if prepared.dispatch != "agent" else prepared.user_message_id,
                prepared.previous_conversation_id,
            )
        )
        self.task = asyncio.create_task(service._execute(self), name="aics.chat.turn")
        service.runtime.inflight.add(self.task)
        self.task.add_done_callback(service.runtime.inflight.discard)

    @property
    def conversation_id(self) -> UUID:
        return self.prepared.conversation_id

    async def events(self) -> AsyncIterator[TurnEvent]:
        while True:
            ev = await self.queue.get()
            if ev is None:
                return
            yield ev

    async def next_event(self, timeout_s: float | None = None) -> TurnEvent | None:
        """One event, or None at the end; raises TimeoutError (SSE heartbeat)."""
        return await asyncio.wait_for(self.queue.get(), timeout_s)

    def cancel(self) -> None:
        """Client disconnected: stop generation unless the answer is already complete."""
        if self._finishing or self._cancel_requested or self.task.done():
            return
        self._cancel_requested = True
        self.task.cancel()

    async def result(self) -> TurnResult:
        """Blocking mode: drain events; raise the AppError of a failed turn."""
        final: TurnResult | None = None
        async for ev in self.events():
            if isinstance(ev, TurnResult):
                final = ev
            elif isinstance(ev, ErrorEvent):
                raise AppError(code=ev.code, message=ev.message, status=ev.status)
        if final is None:  # pragma: no cover - cancelled before any result
            raise AppError(message="对话未完成")
        return final


# ---------------------------------------------------------------- service
class ChatService:
    def __init__(self, runtime: ChatRuntime) -> None:
        self.runtime = runtime

    async def start_turn(self, req: TurnRequest, *, t0: float | None = None) -> TurnHandle:
        """Validate + persist the user message, then start the turn task.

        Raises before anything streams: 404 APP_NOT_FOUND / CONVERSATION_NOT_FOUND,
        422 VALIDATION_ERROR (invalid stored config)."""
        prepared = await self._prepare(req, time.perf_counter() if t0 is None else t0)
        return TurnHandle(self, prepared)

    async def handle_turn(self, req: TurnRequest) -> TurnResult:
        """Blocking helper (eval / tests): one full turn."""
        return await (await self.start_turn(req)).result()

    # ------------------------------------------------------------ prepare
    async def _prepare(self, req: TurnRequest, t0: float) -> _Prepared:
        async with self.runtime.sessions() as s:
            apps = await AppService.load(s)
            app = await apps.get_app(req.app_id)
            cfg = apps.config_of(app)
            convs = ConversationRepository(s)
            msgs = MessageRepository(s)
            previous: UUID | None = None
            conv = None
            if req.conversation_id is not None:
                # Row lock: an idle sweep (chat.lifecycle) closing this conversation either
                # commits first (we see `closed` and start a new one) or waits for our user
                # message, whose last_activity_at then fails its conditional update.
                conv = await convs.get_for_update(req.app_id, req.conversation_id)
                if conv is None:
                    raise ConversationNotFoundError(details={"id": str(req.conversation_id)})
                if conv.status != "active":
                    previous = conv.id
                    conv = await convs.create(
                        req.app_id,
                        NewConversation(
                            channel=conv.channel,  # type: ignore[arg-type]
                            external_user_id=conv.external_user_id,
                            api_key_id=req.api_key_id or conv.api_key_id,
                            created_by=conv.created_by,
                            previous_conversation_id=previous,
                            metadata=dict(req.metadata),
                        ),
                    )
            if conv is None:
                conv = await convs.create(
                    req.app_id,
                    NewConversation(
                        channel=req.channel,  # type: ignore[arg-type]
                        external_user_id=req.external_user_id,
                        api_key_id=req.api_key_id,
                        created_by=req.created_by,
                        metadata=dict(req.metadata),
                    ),
                )
            last = await msgs.last_assistant(req.app_id, conv.id)
            user = await msgs.append(
                req.app_id,
                conv.id,
                NewMessage(role="user", content=req.question),
                custom_dict=app.custom_dict,
            )
            dispatch = dispatch_of(conv.handoff_status, cfg.handoff.handoff_ai_mode)
            reply_id = new_id()
            system_reply = None
            if dispatch == "wait":
                system_reply = cfg.handoff.wait_text
                await msgs.append(
                    req.app_id,
                    conv.id,
                    NewMessage(role="system", kind="handoff_wait", content=system_reply),
                    message_id=reply_id,
                )
            await s.commit()
            return _Prepared(
                req=req,
                t0=t0,
                config=cfg,
                profile=profile_of(app),
                custom_dict=list(app.custom_dict),
                config_version=app.config_version,
                conversation_id=conv.id,
                previous_conversation_id=previous,
                user_message_id=user.id,
                reply_id=reply_id,
                dispatch=dispatch,
                handoff_status=conv.handoff_status,
                refusal_streak=conv.refusal_streak,
                last_assistant_was_clarify=bool(last is not None and last.intent == "clarify"),
                system_reply=system_reply,
                stateless=req.conversation_id is None,
            )

    # ------------------------------------------------------------ execute
    async def _execute(self, h: TurnHandle) -> None:
        p = h.prepared
        try:
            if p.dispatch == "rag":
                await self._run_rag_turn(h)
            else:
                await self._run_silent_turn(h)
        except Exception:  # pragma: no cover - defensive: never leave the consumer hanging
            log.exception("chat.turn_crashed", conversation_id=str(p.conversation_id))
            await h.queue.put(ErrorEvent("INTERNAL_ERROR", "服务内部错误", 500))
        finally:
            if p.stateless:  # 无状态轮次本轮即关：成功 / 报错 / 客户端中断三条路径都收口
                await self._close_stateless(p)
            await h.queue.put(None)

    async def _run_silent_turn(self, h: TurnHandle) -> None:
        """pending+wait (system wait text) or accepted (no reply): no rag call (§3.2)."""
        p = h.prepared
        h._finishing = True
        if p.system_reply is not None:
            await h.queue.put(DeltaEvent(text=p.system_reply))
        await self._after_turn(p, None)
        await self._emit_end(
            h,
            TurnResult(
                conversation_id=p.conversation_id,
                previous_conversation_id=p.previous_conversation_id,
                message_id=p.reply_id if p.dispatch == "wait" else p.user_message_id,
                user_message_id=p.user_message_id,
                reply_by="system" if p.dispatch == "wait" else "none",
                handoff_status=p.handoff_status,
                answer=p.system_reply,
                answer_type=None,
                latency_ms=_ms(p.t0),
            ),
        )

    def _rag_input(self, p: _Prepared) -> RagInput:
        cfg = p.config.rag
        if p.req.rag_overrides:  # 请求级参数只影响本轮（OpenAI 兼容取 min(请求, 应用配置)）
            cfg = cfg.model_copy(update=p.req.rag_overrides)
        return RagInput(
            app_id=p.req.app_id,
            conversation_id=p.conversation_id,
            question=p.req.question,
            inline_history=p.req.inline_history,
            user_message_id=p.user_message_id,
            config=cfg,
            profile=p.profile,
            handoff=p.config.handoff,
            tags=p.req.tags,
            refusal_streak=p.refusal_streak,
            last_assistant_was_clarify=p.last_assistant_was_clarify,
            run_mode=p.req.run_mode,
        )

    async def _run_rag_turn(self, h: TurnHandle) -> None:
        p = h.prepared
        parts: list[str] = []
        first_token_ms: int | None = None
        result: RagResult | None = None
        try:
            async for ev in run_rag(self._rag_input(p), self.runtime.ports()):
                if isinstance(ev, RagResult):
                    result = ev
                    h._finishing = True
                elif isinstance(ev, DeltaEvent):
                    if first_token_ms is None:
                        first_token_ms = _ms(p.t0)
                    parts.append(ev.text)
                    await h.queue.put(ev)
                else:  # CitationsEvent / TraceEvent (debug only)
                    await h.queue.put(ev)
        except asyncio.CancelledError:
            if not h._cancel_requested:
                raise  # shutdown / outer cancellation: not a client disconnect
            await self._persist_interrupted(p, "".join(parts), first_token_ms)
            return
        except (AppError, TimeoutError) as e:
            err = e if isinstance(e, AppError) else UpstreamTimeoutError()
            await self._persist_error(p, "".join(parts), first_token_ms, err)
            await h.queue.put(ErrorEvent(err.code, err.message, err.status))
            return
        if result is None:  # pragma: no cover - run_rag always ends with a result
            raise RuntimeError("run_rag ended without RagResult")
        final = await self._persist_answer(p, result, first_token_ms)
        await self._after_turn(p, result)
        await self._emit_end(h, final)

    async def _emit_end(self, h: TurnHandle, final: TurnResult) -> None:
        metrics.inc(
            "aics_chat_turns_total", reply_by=final.reply_by, answer_type=final.answer_type or "-"
        )
        if final.first_token_ms is not None:
            metrics.observe("aics_chat_first_token_ms", final.first_token_ms)
        await h.queue.put(final)

    # ------------------------------------------------------------ persistence
    async def _persist_answer(
        self, p: _Prepared, res: RagResult, first_token_ms: int | None
    ) -> TurnResult:
        latency = _ms(p.t0)
        citations = [citation_dict(c) for c in res.citations]
        intent = res.intent.model_dump()
        handoff = res.answer_type == "handoff"
        msg = NewMessage(
            role="assistant",
            content=res.answer,
            answer_type=res.answer_type,
            intent=res.intent.name,
            intent_confidence=res.intent.confidence,
            intent_source=res.intent.source,
            slots=dict(res.slots) or None,
            rewritten_query=res.rewritten_query,
            citations=citations,
            retrieval_trace={**res.trace, "timings": res.timings, "usage": res.usage.model_dump()},
            decision_reason=res.decision_reason,
            handoff=handoff,
            prompt_tokens=res.usage.prompt_tokens,
            completion_tokens=res.usage.completion_tokens,
            latency_ms=latency,
            first_token_ms=first_token_ms,
            llm_model=res.llm_model or None,
            prompt_version=res.prompt_version or None,
            degraded=list(res.degraded),
            config_version=p.config_version,
        )
        status = await self._write_assistant(
            p, msg, intent=res.intent.name, handoff_reason=res.handoff_reason
        )
        return TurnResult(
            conversation_id=p.conversation_id,
            previous_conversation_id=p.previous_conversation_id,
            message_id=p.reply_id,
            user_message_id=p.user_message_id,
            reply_by="ai",
            handoff_status=status,
            answer=res.answer,
            answer_type=res.answer_type,
            handoff=handoff,
            handoff_reason=res.handoff_reason if handoff else None,
            intent=intent,
            citations=citations,
            prompt_tokens=res.usage.prompt_tokens,
            completion_tokens=res.usage.completion_tokens,
            latency_ms=latency,
            first_token_ms=first_token_ms,
            degraded=list(res.degraded),
        )

    async def _persist_interrupted(self, p: _Prepared, text: str, first: int | None) -> None:
        # 03-RAG流程设计 §6: answer_type follows the decision already made (normally answer).
        msg = NewMessage(
            role="assistant",
            content=text,
            answer_type="answer",
            decision_reason="client_disconnected",
            latency_ms=_ms(p.t0),
            first_token_ms=first,
            config_version=p.config_version,
            interrupted=True,
        )
        await self._write_assistant(p, msg, intent=None, handoff_reason=None)
        metrics.inc("aics_chat_turns_total", reply_by="ai", answer_type="interrupted")
        log.info("chat.turn_interrupted", conversation_id=str(p.conversation_id), chars=len(text))

    async def _persist_error(
        self, p: _Prepared, text: str, first: int | None, err: AppError
    ) -> None:
        msg = NewMessage(
            role="assistant",
            content=text,
            answer_type="error",
            decision_reason=err.code,
            latency_ms=_ms(p.t0),
            first_token_ms=first,
            config_version=p.config_version,
        )
        await self._write_assistant(p, msg, intent=None, handoff_reason=None)
        metrics.inc("aics_chat_turns_total", reply_by="ai", answer_type="error")
        log.warning("chat.turn_failed", conversation_id=str(p.conversation_id), code=err.code)

    async def _write_assistant(
        self, p: _Prepared, msg: NewMessage, *, intent: str | None, handoff_reason: str | None
    ) -> str:
        """Assistant message + conversation state in one transaction; returns handoff_status."""
        app_id, cid = p.req.app_id, p.conversation_id
        async with self.runtime.sessions() as s:
            msgs = MessageRepository(s)
            # append locks the conversation row (FOR UPDATE) for the rest of the transaction,
            # so the streak / handoff update below sees concurrent turns serialized.
            await msgs.append(
                app_id,
                cid,
                msg,
                custom_dict=p.custom_dict,
                allow_closed=True,
                message_id=p.reply_id,
            )
            convs = ConversationRepository(s)
            conv = await convs.get_or_404(app_id, cid)
            values: dict[str, Any] = {}
            streak = next_refusal_streak(
                conv.refusal_streak,
                intent=intent or "",
                answer_type=msg.answer_type or "",
                interrupted=msg.interrupted,
            )
            if streak != conv.refusal_streak:
                values["refusal_streak"] = streak
            status = conv.handoff_status
            if msg.handoff and not msg.interrupted and status in ("none", "released"):
                # §3.1: none / released → pending (service hours / webhooks: M3.4 / M3.5).
                status = "pending"
                values.update(
                    handoff_status=status,
                    handoff_reason=handoff_reason,
                    handoff_requested_at=func.now(),
                )
            if values:
                await convs.update(app_id, cid, **values)
            await s.commit()
            return status

    async def _after_turn(self, p: _Prepared, res: RagResult | None) -> None:
        """Facts from slots, then the compaction check (对话上下文管理 §7 / §8.2).

        Memory problems never fail a turn that has already been answered."""
        if p.stateless:
            return  # 无状态轮次不写事实、不压缩：历史随请求来，会话本轮即关
        cfg = p.config.rag
        memory = self.runtime.memory
        try:
            if res is not None:
                await memory.capture_slots(
                    p.req.app_id,
                    p.conversation_id,
                    res.slots,
                    intent_source=res.intent.source,
                    intent_confidence=res.intent.confidence,
                    user_message_id=p.user_message_id,
                    enable_facts=cfg.enable_facts,
                )
            params = CompactParams(
                keep_recent_turns=cfg.keep_recent_turns,
                compact_trigger_tokens=cfg.compact_trigger_tokens,
                enable_facts=cfg.enable_facts,
                model=cfg.summary_model or cfg.llm_model,
            )
            await memory.maybe_compact(
                p.req.app_id,
                p.conversation_id,
                params,
                run_mode=p.req.run_mode.value,
                flags=list(res.degraded) if res is not None else [],
            )
        except Exception as e:
            log.warning(
                "chat.memory_after_turn_failed",
                conversation_id=str(p.conversation_id),
                error_type=type(e).__name__,
            )

    async def _close_stateless(self, p: _Prepared) -> None:
        """无状态轮次收尾：本轮建出的会话立即关闭（`close_reason=stateless`，设计讨论 0009 D7）。

        条件单行更新，重复调用无副作用；这类会话已关闭，不会被 `idle_sweep` 扫到、也不会产生
        空闲提醒。关闭失败只记日志——回答已经落库，不影响本轮结果。"""
        try:
            async with self.runtime.sessions() as s:
                closed = await ConversationRepository(s).close(
                    p.req.app_id, p.conversation_id, reason="stateless"
                )
                await s.commit()
            if closed:
                log.info("chat.stateless_closed", conversation_id=str(p.conversation_id))
        except Exception as e:
            log.warning(
                "chat.stateless_close_failed",
                conversation_id=str(p.conversation_id),
                error_type=type(e).__name__,
            )


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


__all__ = [
    "CHANNEL_OF_MODE",
    "ChatService",
    "ErrorEvent",
    "StartEvent",
    "TurnEvent",
    "TurnHandle",
    "TurnRequest",
    "TurnResult",
    "citation_dict",
    "dispatch_of",
    "profile_of",
]
