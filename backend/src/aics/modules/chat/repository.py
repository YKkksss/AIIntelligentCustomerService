"""conversations / messages repositories used by chat.service (M2.1).

`chat.service` is the only writer of messages (对话上下文管理 §0); aics.memory reads them and
owns the `summary*` columns and the fact tables. Both repositories inherit AppScopedRepository,
so every query is bound to one app_id.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, tuple_, update

from aics.core.errors import AppError, ConversationNotFoundError
from aics.core.textsearch import tokenize_for_index
from aics.core.tokens import count_tokens
from aics.db.models import Conversation, Message
from aics.db.scoped import MAX_PAGE_SIZE, AppScopedRepository
from aics.modules.chat.schemas import TITLE_MAX_CHARS, NewConversation, NewMessage

__all__ = [
    "ConversationClosedError",
    "ConversationNotFoundError",
    "ConversationRepository",
    "MessageRepository",
    "message_search_text",
]


class ConversationClosedError(AppError):
    code, status, message = "CONVERSATION_CLOSED", 409, "会话已关闭"


def message_search_text(app_id: UUID, role: str, content: str, custom_dict: Sequence[str]) -> str:
    """`messages.search_text`: jieba tokens of the app (same scheme as chunks).

    System messages (idle notices, handoff notices) are platform boilerplate, not conversation
    content, so they are not indexed for in-conversation recall.
    """
    if role == "system" or not content:
        return ""
    return tokenize_for_index(app_id, content, custom_dict)


class ConversationRepository(AppScopedRepository[Conversation]):
    model = Conversation

    async def create(self, app_id: UUID, data: NewConversation) -> Conversation:
        prev = data.previous_conversation_id
        if prev is not None and await self.get(app_id, prev) is None:
            raise ConversationNotFoundError(details={"id": str(data.previous_conversation_id)})
        conv = Conversation(
            app_id=app_id,
            channel=data.channel,
            external_user_id=data.external_user_id,
            api_key_id=data.api_key_id,
            created_by=data.created_by,
            previous_conversation_id=data.previous_conversation_id,
            metadata_=dict(data.metadata),
        )
        return await self.add(app_id, conv)

    async def get_or_404(self, app_id: UUID, conversation_id: UUID) -> Conversation:
        conv = await self.get(app_id, conversation_id)
        if conv is None:
            raise ConversationNotFoundError(details={"id": str(conversation_id)})
        return conv

    async def get_for_update(self, app_id: UUID, conversation_id: UUID) -> Conversation | None:
        stmt = self._scoped(app_id).where(Conversation.id == conversation_id).with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    # ------------------------------------------------------------ idle sweep (M2.7)
    async def idle_app_ids_all_apps(self, *, before: datetime) -> list[UUID]:
        """Apps that have an active, non-eval conversation idle since `before` (cross-app:
        only `chat.lifecycle.sweep` calls this; uses ix_conversations_active_activity)."""
        stmt = (
            select(Conversation.app_id)
            .where(
                Conversation.status == "active",
                Conversation.channel != "eval",
                Conversation.last_activity_at <= before,
            )
            .distinct()
        )
        return list((await self.session.execute(stmt)).scalars())

    async def idle_candidates(
        self,
        app_id: UUID,
        *,
        before: datetime,
        after: datetime | None = None,
        unnoticed: bool = False,
        pause_pending: bool = False,
        limit: int = 500,
    ) -> list[tuple[UUID, datetime]]:
        """(id, last_activity_at) of active conversations idle since `before` (and active after
        `after`), excluding eval and paused ones (会话生命周期 §3.4), oldest first."""
        stmt = self._scoped(app_id).where(*self._sweepable(pause_pending))
        stmt = stmt.where(Conversation.last_activity_at <= before)
        if after is not None:
            stmt = stmt.where(Conversation.last_activity_at > after)
        if unnoticed:
            stmt = stmt.where(Conversation.idle_notice_sent_at.is_(None))
        stmt = stmt.order_by(Conversation.last_activity_at).limit(limit)
        rows = await self.session.execute(
            stmt.with_only_columns(Conversation.id, Conversation.last_activity_at)
        )
        return [(r[0], r[1]) for r in rows]

    async def mark_idle_notice(
        self,
        app_id: UUID,
        conversation_id: UUID,
        *,
        seen_activity: datetime,
        at: datetime,
        pause_pending: bool = False,
    ) -> bool:
        """Set idle_notice_sent_at only if nothing happened since the sweep read the row."""
        stmt = (
            update(Conversation)
            .where(
                Conversation.app_id == app_id,
                Conversation.id == conversation_id,
                Conversation.last_activity_at == seen_activity,
                Conversation.idle_notice_sent_at.is_(None),
                *self._sweepable(pause_pending),
            )
            .values(idle_notice_sent_at=at)
        )
        return bool((await self.session.execute(stmt)).rowcount)  # type: ignore[attr-defined]

    async def close_idle(
        self,
        app_id: UUID,
        conversation_id: UUID,
        *,
        seen_activity: datetime,
        at: datetime,
        pause_pending: bool = False,
    ) -> bool:
        """Close for idleness only if nothing happened since the sweep read the row."""
        stmt = (
            update(Conversation)
            .where(
                Conversation.app_id == app_id,
                Conversation.id == conversation_id,
                Conversation.last_activity_at == seen_activity,
                *self._sweepable(pause_pending),
            )
            .values(status="closed", close_reason="idle", closed_at=at)
        )
        return bool((await self.session.execute(stmt)).rowcount)  # type: ignore[attr-defined]

    @staticmethod
    def _sweepable(pause_pending: bool) -> list[Any]:
        paused = ["accepted", "pending"] if pause_pending else ["accepted"]
        return [
            Conversation.status == "active",
            Conversation.channel != "eval",
            Conversation.handoff_status.not_in(paused),
        ]

    async def list_recent(
        self,
        app_id: UUID,
        *,
        channel: str | None = None,
        status: str | None = None,
        external_user_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[Conversation]:
        """Newest activity first (uses ix_conversations_app_last_message)."""
        stmt = self._scoped(app_id)
        if channel is not None:
            stmt = stmt.where(Conversation.channel == channel)
        if status is not None:
            stmt = stmt.where(Conversation.status == status)
        if external_user_id is not None:
            stmt = stmt.where(Conversation.external_user_id == external_user_id)
        stmt = (
            stmt.order_by(Conversation.last_message_at.desc(), Conversation.id.desc())
            .limit(min(max(limit, 1), MAX_PAGE_SIZE))
            .offset(max(offset, 0))
        )
        return list((await self.session.execute(stmt)).scalars())

    async def close(
        self, app_id: UUID, conversation_id: UUID, *, reason: str, at: datetime | None = None
    ) -> bool:
        """Close an active conversation; returns False if it was already closed (idempotent)."""
        stmt = (
            update(Conversation)
            .where(
                Conversation.app_id == app_id,
                Conversation.id == conversation_id,
                Conversation.status == "active",
            )
            .values(status="closed", close_reason=reason, closed_at=at or func.now())
        )
        return bool((await self.session.execute(stmt)).rowcount)  # type: ignore[attr-defined]


class MessageRepository(AppScopedRepository[Message]):
    model = Message

    async def append(
        self,
        app_id: UUID,
        conversation_id: UUID,
        data: NewMessage,
        *,
        custom_dict: Sequence[str] = (),
        allow_closed: bool = False,
        message_id: UUID | None = None,
    ) -> Message:
        """Insert one message and update the conversation counters in the same transaction.

        `token_count` (core.tokens) and `search_text` (the app's jieba tokenizer, `custom_dict`
        = apps.custom_dict) are always computed here; callers cannot pass them.
        The conversation row is locked so that concurrent appends keep counters consistent.
        `message_id` lets chat.service announce the reply id (SSE `start`) before generation.
        """
        conv_repo = ConversationRepository(self.session)
        conv = await conv_repo.get_for_update(app_id, conversation_id)
        if conv is None:
            raise ConversationNotFoundError(details={"id": str(conversation_id)})
        # System notices (e.g. idle_closed) may be written right after closing.
        if conv.status != "active" and not allow_closed and data.role != "system":
            raise ConversationClosedError(details={"id": str(conversation_id)})

        msg = Message(
            **({"id": message_id} if message_id is not None else {}),
            app_id=app_id,
            conversation_id=conversation_id,
            token_count=count_tokens(data.content),
            search_text=message_search_text(app_id, data.role, data.content, custom_dict),
            **data.model_dump(),
        )
        self.session.add(msg)
        await self.session.flush()

        values: dict[str, Any] = {
            "message_count": Conversation.message_count + 1,
            "last_message_at": msg.created_at,
        }
        # last_activity_at drives the idle timer; system messages must not reset it.
        if data.role != "system":
            values["last_activity_at"] = msg.created_at
            values["idle_notice_sent_at"] = None
        if data.role == "user" and conv.title is None:
            values["title"] = data.content.strip()[:TITLE_MAX_CHARS]
        await self.session.execute(
            update(Conversation)
            .where(Conversation.app_id == app_id, Conversation.id == conversation_id)
            .values(**values)
        )
        return msg

    async def list_for_conversation(
        self,
        app_id: UUID,
        conversation_id: UUID,
        *,
        limit: int = 50,
        before: UUID | None = None,
    ) -> list[Message]:
        """Messages oldest → newest; `before` pages backwards from a message id."""
        stmt = self._scoped(app_id).where(Message.conversation_id == conversation_id)
        if before is not None:
            anchor = (
                select(Message.created_at)
                .where(Message.app_id == app_id, Message.id == before)
                .scalar_subquery()
            )
            stmt = stmt.where(Message.created_at < anchor)
        stmt = stmt.order_by(Message.created_at.desc(), Message.id.desc()).limit(
            min(max(limit, 1), MAX_PAGE_SIZE)
        )
        rows = list((await self.session.execute(stmt)).scalars())
        rows.reverse()
        return rows

    async def page_for_conversation(
        self, app_id: UUID, conversation_id: UUID, *, limit: int, before: UUID | None = None
    ) -> tuple[list[Message], bool]:
        """Open-API cursor page: the `limit` newest messages strictly before `before`
        (ordered by `(created_at, id)`), returned oldest → newest, plus whether older exist."""
        stmt = self._scoped(app_id).where(Message.conversation_id == conversation_id)
        if before is not None:
            anchor = (
                select(Message.created_at, Message.id)
                .where(
                    Message.app_id == app_id,
                    Message.conversation_id == conversation_id,
                    Message.id == before,
                )
                .subquery()
            )
            stmt = stmt.where(
                tuple_(Message.created_at, Message.id)
                < select(anchor.c.created_at, anchor.c.id).scalar_subquery()
            )
        size = min(max(limit, 1), MAX_PAGE_SIZE)
        stmt = stmt.order_by(Message.created_at.desc(), Message.id.desc()).limit(size + 1)
        rows = list((await self.session.execute(stmt)).scalars())
        more = len(rows) > size
        rows = rows[:size]
        rows.reverse()
        return rows, more

    async def last_assistant(self, app_id: UUID, conversation_id: UUID) -> Message | None:
        """Latest assistant message (clarify follow-up detection, 意图识别 §5.2.2)."""
        stmt = (
            self._scoped(app_id)
            .where(Message.conversation_id == conversation_id, Message.role == "assistant")
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def update(self, app_id: UUID, id: UUID, /, **values: Any) -> int:
        """Messages are immutable except for retention clean-up of the trace."""
        allowed = {"retrieval_trace"}
        if not set(values) <= allowed:
            raise ValueError(f"messages only allow updating {sorted(allowed)}")
        return await super().update(app_id, id, **values)
