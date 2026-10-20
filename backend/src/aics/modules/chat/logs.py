"""会话日志、待回访与后台强制释放（M4.5 ③）。

运营面要按「渠道 / 状态 / 转人工状态 / 结束原因 / 是否待回访 / 意图 / 是否点踩 / 时间 / 关键字」
检索会话，并能打开一条会话看完整时间线（消息、引用、trace、摘要、事实）——这是只读侧；
写侧只有两个动作：后台强制释放（`handoff_status` `pending|accepted → released`，结束原因
`admin`）与待回访「标记已回访」。

设计要点
- **模块独立性**：`chat` 不能 import `handoff`（ADR-0017），所以强制释放在这里直接改
  `conversations` 的转人工四列，并复用 `chat.events` + `chat.payloads` 发同一条
  `handoff.released` 事件（`released_by="admin"`、`reason="admin"`），与坐席侧的事件契约一致。
- **关键字搜索走 `messages.tsv`**（GIN 索引 + `plainto_tsquery('simple', …)`），不用 `ILIKE`：
  `search_text` 是写路径维护的检索文本，`tsv` 是生成列（数据库设计 §3.9）。
- **游标**：会话列表按 `(last_activity_at, id)` 降序、待回访按 `(callback_requested_at, id)`
  降序；游标是 `base64url(JSON{"t": iso, "id": uuid})`，用 `(t, id) < (cursor)` 做「更早一页」，
  同一时间戳的会话不会因分页被跳过（M4.5 计划 ③、ISSUE-87）。
- 时间一律 UTC、出参 ISO 字符串；`mask=True`（只有 `app.write` 成员能关）时正文与 trace 里的
  手机号等标识打码——与调试对话的 viewer 口径一致。
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import ColumnElement, and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aics.core.audit import AuditEntry, AuditSink, diff
from aics.core.errors import CallbackStateConflictError, ConflictError, ValidationAppError
from aics.core.masking import mask_text
from aics.db.models.conversation import Conversation, Message
from aics.db.models.fact import ConversationFact
from aics.db.models.feedback import Feedback
from aics.modules.apps.service import AppService
from aics.modules.chat import events
from aics.modules.chat.repository import ConversationRepository, MessageRepository
from aics.modules.chat.service import append_agent_message
from aics.modules.webhooks import payloads

log = structlog.get_logger(__name__)

# 运营日志默认只看线上渠道；`channel=debug` / `channel=eval` 显式传才出现（调试与评测不污染运营面）。
DEFAULT_CHANNELS = ("api", "openai")
LOG_CHANNELS = ("api", "openai", "debug", "eval")

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
DETAIL_MESSAGES = 200

OPEN_HANDOFF = ("pending", "accepted")
ADMIN_END_REASON = "admin"
RELEASE_ACTION = "chat.conversation.release"
CALLBACK_ACTION = "chat.callback.clear"


# --------------------------------------------------------------------------- 游标


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def encode_cursor(at: datetime | None, row_id: UUID) -> str | None:
    """`(时间, id)` → base64url 字符串；没有时间戳的行（如从未活动）不给游标。"""
    if at is None:
        return None
    raw = json.dumps({"t": at.isoformat(), "id": str(row_id)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(raw: str | None) -> tuple[datetime, UUID] | None:
    """解析游标；空值返回 `None`，非法值抛 422（`loc=["cursor"]`）。"""
    if not raw:
        return None
    try:
        padded = raw + "=" * (-len(raw) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        return datetime.fromisoformat(str(payload["t"])), UUID(str(payload["id"]))
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeDecodeError):
        raise ValidationAppError(
            message="游标无效",
            details={"errors": [{"loc": ["cursor"], "msg": "无法解析的分页游标"}]},
        ) from None


def _older_than(
    column: Any, row_id: Any, cursor: tuple[datetime, UUID] | None
) -> ColumnElement[bool] | None:
    if cursor is None:
        return None
    at, cid = cursor
    return or_(column < at, and_(column == at, row_id < cid))


# --------------------------------------------------------------------------- 过滤


@dataclass(frozen=True)
class LogFilter:
    """会话日志的筛选条件；`channels` 为空表示不限渠道。"""

    channels: tuple[str, ...] = DEFAULT_CHANNELS
    status: str | None = None
    handoff_status: str | None = None
    close_reason: str | None = None
    needs_callback: bool | None = None
    intents: tuple[str, ...] = ()
    downvoted: bool | None = None
    since: datetime | None = None
    until: datetime | None = None
    q: str | None = None


# --------------------------------------------------------------------------- 行映射


def _conversation_row(
    conv: Conversation, *, intent: str | None = None, downvotes: int = 0
) -> dict[str, Any]:
    return {
        "id": str(conv.id),
        "channel": conv.channel,
        "status": conv.status,
        "title": conv.title,
        "external_user_id": conv.external_user_id,
        "created_at": _iso(conv.created_at),
        "last_activity_at": _iso(conv.last_activity_at),
        "closed_at": _iso(conv.closed_at),
        "close_reason": conv.close_reason,
        "message_count": conv.message_count,
        "handoff_status": conv.handoff_status,
        "handoff_reason": conv.handoff_reason,
        "handoff_requested_at": _iso(conv.handoff_requested_at),
        "handoff_end_reason": conv.handoff_end_reason,
        "handoff_agent_name": conv.handoff_agent_name,
        "needs_callback": conv.needs_callback,
        "callback_reason": conv.callback_reason,
        "callback_requested_at": _iso(conv.callback_requested_at),
        "callback_cleared_at": _iso(conv.callback_cleared_at),
        "callback_note": conv.callback_note,
        "intent": intent,
        "downvotes": downvotes,
    }


def _masked(value: Any) -> Any:
    """递归打码：字符串走 `mask_text`，容器就地遍历。"""
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, dict):
        return {k: _masked(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_masked(v) for v in value]
    return value


def _message_row(message: Message, *, mask: bool) -> dict[str, Any]:
    return {
        "id": str(message.id),
        "role": message.role,
        "kind": message.kind,
        "agent_id": message.agent_id,
        "content": mask_text(message.content) if mask else message.content,
        "created_at": _iso(message.created_at),
        "answer_type": message.answer_type,
        "intent": message.intent,
        "handoff": message.handoff,
        "degraded": bool(message.degraded),
        "citations": _masked(message.citations) if mask else message.citations,
    }


def feedback_row(
    feedback: Feedback, *, message: Message | None = None, mask: bool = False
) -> dict[str, Any]:
    """一条反馈（列表与「标记已处理」共用）：带消息摘要，方便运营直接看上下文。"""
    content = None if message is None else message.content
    return {
        "id": str(feedback.id),
        "message_id": str(feedback.message_id),
        "conversation_id": str(message.conversation_id) if message is not None else None,
        "rating": feedback.rating,
        "comment": None
        if feedback.comment is None
        else (mask_text(feedback.comment) if mask else feedback.comment),
        "source": feedback.source,
        "handled": feedback.handled,
        "created_at": _iso(feedback.created_at),
        "message": None
        if content is None
        else {
            "role": message.role if message is not None else None,
            "excerpt": (mask_text(content) if mask else content)[:240],
        },
    }


# --------------------------------------------------------------------------- 服务


@dataclass
class ConversationLogs:
    """会话日志的读写入口；一个实例对应一个数据库会话。"""

    session: AsyncSession
    redis: Any = None
    redis_prefix: str = "aics:"
    audit: AuditSink | None = None

    @property
    def conversations(self) -> ConversationRepository:
        return ConversationRepository(self.session)

    # ------------------------------------------------------------------ 列表

    async def list(
        self,
        app_id: UUID,
        *,
        filters: LogFilter,
        cursor: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        limit = max(1, min(limit, MAX_LIMIT))
        stmt = select(Conversation).where(Conversation.app_id == app_id)
        stmt = stmt.where(*self._conditions(app_id, filters))
        older = _older_than(Conversation.last_activity_at, Conversation.id, decode_cursor(cursor))
        if older is not None:
            stmt = stmt.where(older)
        stmt = stmt.order_by(Conversation.last_activity_at.desc(), Conversation.id.desc())
        rows = list((await self.session.execute(stmt.limit(limit + 1))).scalars())
        has_more = len(rows) > limit
        rows = rows[:limit]
        ids = [c.id for c in rows]
        intents = await self._last_intents(app_id, ids)
        downvotes = await self._downvotes(app_id, ids)
        items = [
            _conversation_row(c, intent=intents.get(c.id), downvotes=downvotes.get(c.id, 0))
            for c in rows
        ]
        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            next_cursor = encode_cursor(last.last_activity_at, last.id)
        return {"items": items, "next_cursor": next_cursor}

    def _conditions(self, app_id: UUID, filters: LogFilter) -> list[ColumnElement[bool]]:
        out: list[ColumnElement[bool]] = []
        if filters.channels:
            out.append(Conversation.channel.in_(filters.channels))
        if filters.status:
            out.append(Conversation.status == filters.status)
        if filters.handoff_status:
            out.append(Conversation.handoff_status == filters.handoff_status)
        if filters.close_reason:
            out.append(Conversation.close_reason == filters.close_reason)
        if filters.needs_callback is not None:
            out.append(Conversation.needs_callback.is_(filters.needs_callback))
        if filters.since is not None:
            out.append(Conversation.last_activity_at >= filters.since)
        if filters.until is not None:
            out.append(Conversation.last_activity_at <= filters.until)
        if filters.intents:
            out.append(
                Conversation.id.in_(
                    select(Message.conversation_id).where(
                        Message.app_id == app_id, Message.intent.in_(filters.intents)
                    )
                )
            )
        if filters.downvoted is not None:
            negated = (
                select(Message.conversation_id)
                .join(Feedback, Feedback.message_id == Message.id)
                .where(Message.app_id == app_id, Feedback.rating == -1)
            )
            out.append(
                Conversation.id.in_(negated)
                if filters.downvoted
                else Conversation.id.not_in(negated)
            )
        if filters.q:
            out.append(
                Conversation.id.in_(
                    select(Message.conversation_id).where(
                        Message.app_id == app_id,
                        Message.tsv.op("@@")(func.plainto_tsquery("simple", filters.q)),
                    )
                )
            )
        return out

    async def _last_intents(self, app_id: UUID, ids: list[UUID]) -> dict[UUID, str]:
        """每个会话最后一条用户消息的意图（列表页展示用，一次查完避免 N+1）。"""
        if not ids:
            return {}
        ranked = (
            select(
                Message.conversation_id.label("conversation_id"),
                Message.intent.label("intent"),
                func.row_number()
                .over(partition_by=Message.conversation_id, order_by=Message.id.desc())
                .label("rn"),
            )
            .where(
                Message.app_id == app_id,
                Message.conversation_id.in_(ids),
                Message.role == "assistant",
                Message.intent.is_not(None),
            )
            .subquery()
        )
        stmt = select(ranked.c.conversation_id, ranked.c.intent).where(ranked.c.rn == 1)
        return {cid: intent for cid, intent in (await self.session.execute(stmt)).all()}

    async def _downvotes(self, app_id: UUID, ids: list[UUID]) -> dict[UUID, int]:
        if not ids:
            return {}
        stmt = (
            select(Message.conversation_id, func.count())
            .join(Feedback, Feedback.message_id == Message.id)
            .where(
                Message.app_id == app_id,
                Message.conversation_id.in_(ids),
                Feedback.rating == -1,
            )
            .group_by(Message.conversation_id)
        )
        return {cid: int(count) for cid, count in (await self.session.execute(stmt)).all()}

    # ------------------------------------------------------------------ 详情

    async def detail(
        self, app_id: UUID, conversation_id: UUID, *, mask: bool = False
    ) -> dict[str, Any]:
        conv = await self.conversations.get_or_404(app_id, conversation_id)
        messages = await MessageRepository(self.session).list_for_conversation(
            app_id, conversation_id, limit=DETAIL_MESSAGES
        )
        intents = await self._last_intents(app_id, [conv.id])
        downvotes = await self._downvotes(app_id, [conv.id])
        summary = conv.summary
        facts = await self._facts(app_id, conversation_id)
        return {
            "conversation": _conversation_row(
                conv, intent=intents.get(conv.id), downvotes=downvotes.get(conv.id, 0)
            ),
            "messages": [_message_row(m, mask=mask) for m in messages],
            "summary": None
            if not summary
            else {
                "value": _masked(summary) if mask else summary,
                "version": conv.summary_version,
                "until_msg_id": None
                if conv.summary_until_msg_id is None
                else str(conv.summary_until_msg_id),
                "updated_at": _iso(conv.summary_updated_at),
            },
            "facts": [_masked(f) if mask else f for f in facts],
        }

    async def _facts(self, app_id: UUID, conversation_id: UUID) -> list[dict[str, Any]]:
        stmt = (
            select(ConversationFact)
            .where(
                ConversationFact.app_id == app_id,
                ConversationFact.conversation_id == conversation_id,
                ConversationFact.retracted_at.is_(None),
            )
            .order_by(ConversationFact.first_seen_at)
        )
        rows = list((await self.session.execute(stmt)).scalars())
        return [
            {
                "key": f.key,
                "value": f.value,
                "category": f.category,
                "confidence": f.confidence,
                "version": f.version,
                "first_seen_at": _iso(f.first_seen_at),
            }
            for f in rows
        ]

    # ------------------------------------------------------------------ 强制释放

    async def force_release(
        self,
        app_id: UUID,
        conversation_id: UUID,
        *,
        actor_id: UUID | None = None,
        note: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """后台把 `pending|accepted` 直接置 `released`，结束原因 `admin`。

        与坐席侧 `handoff.service.release()` 的差别只在「谁释放」：这里不要求坐席身份，
        也不需要 `pending` 时的归属校验；条件单行 UPDATE 抢状态，抢不到抛 409。
        """
        conv = await self.conversations.get_or_404(app_id, conversation_id)
        before = conv.handoff_status
        now = datetime.now(UTC)
        stmt = (
            update(Conversation)
            .where(
                Conversation.app_id == app_id,
                Conversation.id == conversation_id,
                Conversation.handoff_status.in_(OPEN_HANDOFF),
            )
            .values(
                handoff_status="released",
                handoff_released_at=now,
                handoff_end_reason=ADMIN_END_REASON,
            )
            .returning(Conversation.id)
            .execution_options(synchronize_session=False)
        )
        if (await self.session.execute(stmt)).scalar_one_or_none() is None:
            raise ConflictError(
                message="会话当前不在转人工中",
                code="HANDOFF_STATE_CONFLICT",
                details={"handoff_status": before},
            )
        target = events.Target.of(conv)
        await events.emit(
            self.session,
            target,
            "handoff.released",
            payloads.handoff_released(
                agent_id=conv.handoff_agent_id,
                note=note,
                released_by="admin",
                reason=ADMIN_END_REASON,
            ),
        )
        cfg = await AppService(self.session).get_config(app_id)
        if cfg.handoff.released_text:
            notice = await append_agent_message(
                self.session,
                app_id,
                conversation_id,
                role="system",
                kind="handoff_released",
                content=cfg.handoff.released_text,
            )
            await events.emit(
                self.session,
                target,
                "message.created",
                payloads.message_created(notice),
            )
        await self._audit(
            RELEASE_ACTION,
            app_id,
            target_id=conversation_id,
            actor_id=actor_id,
            meta=meta,
            payload=diff({"handoff_status": before}, {"handoff_status": "released"}),
        )
        await self.session.commit()
        await events.publish_pending(self.session, self._publisher())
        await self.session.refresh(conv)
        if note:
            # 备注只进日志：conversations 没有「释放备注」列（M3.4 ④ 的决定，ISSUE-53）。
            log.info(
                "chat.conversation.admin_release",
                app_id=str(app_id),
                conversation_id=str(conversation_id),
                note=note,
            )
        return _conversation_row(conv)

    # ------------------------------------------------------------------ 待回访

    async def callbacks(
        self,
        app_id: UUID,
        *,
        cursor: str | None = None,
        limit: int = DEFAULT_LIMIT,
        include_cleared: bool = False,
    ) -> dict[str, Any]:
        limit = max(1, min(limit, MAX_LIMIT))
        if include_cleared:
            # 已回访的也列出来时，按「请求时间」倒序把两类合在一起。
            stmt = select(Conversation).where(
                Conversation.app_id == app_id,
                or_(
                    Conversation.needs_callback.is_(True),
                    Conversation.callback_cleared_at.is_not(None),
                ),
            )
        else:
            stmt = select(Conversation).where(
                Conversation.app_id == app_id, Conversation.needs_callback.is_(True)
            )
        older = _older_than(
            Conversation.callback_requested_at, Conversation.id, decode_cursor(cursor)
        )
        if older is not None:
            stmt = stmt.where(older)
        stmt = stmt.order_by(
            Conversation.callback_requested_at.desc().nullslast(), Conversation.id.desc()
        )
        rows = list((await self.session.execute(stmt.limit(limit + 1))).scalars())
        has_more = len(rows) > limit
        rows = rows[:limit]
        ids = [c.id for c in rows]
        intents = await self._last_intents(app_id, ids)
        downvotes = await self._downvotes(app_id, ids)
        items = [
            _conversation_row(c, intent=intents.get(c.id), downvotes=downvotes.get(c.id, 0))
            for c in rows
        ]
        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            next_cursor = encode_cursor(last.callback_requested_at, last.id)
        return {"items": items, "next_cursor": next_cursor}

    async def clear_callback(
        self,
        app_id: UUID,
        conversation_id: UUID,
        *,
        note: str | None = None,
        actor_id: UUID | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """「标记已回访」：只在 `needs_callback=true` 时生效，记录时间、操作人与备注。"""
        conv = await self.conversations.get_or_404(app_id, conversation_id)
        if not conv.needs_callback:
            raise CallbackStateConflictError(details={"needs_callback": False})
        now = datetime.now(UTC)
        stmt = (
            update(Conversation)
            .where(
                Conversation.app_id == app_id,
                Conversation.id == conversation_id,
                Conversation.needs_callback.is_(True),
            )
            .values(needs_callback=False, callback_cleared_at=now, callback_cleared_by=actor_id)
            .returning(Conversation.id)
            .execution_options(synchronize_session=False)
        )
        if (await self.session.execute(stmt)).scalar_one_or_none() is None:
            raise CallbackStateConflictError(details={"needs_callback": False})
        if note:
            await self.session.execute(
                update(Conversation)
                .where(Conversation.app_id == app_id, Conversation.id == conversation_id)
                .values(callback_note=note)
                .execution_options(synchronize_session=False)
            )
        await self._audit(
            CALLBACK_ACTION,
            app_id,
            target_id=conversation_id,
            actor_id=actor_id,
            meta=meta,
            payload={
                "needs_callback": [True, False],
                **({} if not note else {"note": [None, note]}),
            },
        )
        await self.session.commit()
        await self.session.refresh(conv)
        return _conversation_row(conv)

    # ------------------------------------------------------------------ 内部

    def _publisher(self) -> events.Publisher | None:
        if self.redis is None:
            return None
        return events.RedisPublisher(self.redis, prefix=self.redis_prefix)

    async def _audit(
        self,
        action: str,
        app_id: UUID,
        *,
        target_id: UUID,
        actor_id: UUID | None,
        meta: dict[str, Any] | None,
        payload: dict[str, Any] | None,
    ) -> None:
        if self.audit is None or actor_id is None:
            return
        await self.audit.write(
            self.session,
            AuditEntry(
                action=action,
                actor_type="user",
                actor_id=actor_id,
                app_id=app_id,
                target_type="conversation",
                target_id=target_id,
                diff=payload,
                ip=(meta or {}).get("ip"),
                user_agent=(meta or {}).get("user_agent"),
                request_id=(meta or {}).get("request_id"),
            ),
        )
