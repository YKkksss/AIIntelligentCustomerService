"""PostgreSQL implementation of ConversationStore (对话上下文管理 §0, §5).

Repositories inherit AppScopedRepository (every query carries app_id). memory only reads
`messages`; it reads/writes `conversations.summary*`, `conversation_facts` and appends to
`conversation_fact_history`.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, literal_column, select, update
from sqlalchemy.dialects.postgresql import insert

from aics.core.errors import ConversationNotFoundError, ValidationAppError
from aics.db.models import Conversation, ConversationFact, ConversationFactHistory, Message
from aics.db.scoped import AppScopedRepository
from aics.memory.schemas import (
    SUMMARY_HISTORY_KEEP,
    ConversationSummary,
    Fact,
    FactChange,
    FactSource,
    FactWrite,
    Hit,
    StoredMessage,
    SummaryState,
    Turn,
    fact_category,
    fact_importance,
    group_turns,
)
from aics.memory.store import (
    MAX_TURN_MESSAGES,
    FactExistsError,
    FactNotFoundError,
    check_confidence,
    check_k,
    check_update,
)


# ---------------------------------------------------------------- repositories
class ConversationSummaryRepository(AppScopedRepository[Conversation]):
    """Only the summary* columns of conversations (chat owns the rest of the row)."""

    model = Conversation

    async def get_for_update(self, app_id: UUID, conversation_id: UUID) -> Conversation | None:
        stmt = self._scoped(app_id).where(Conversation.id == conversation_id).with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def update(self, app_id: UUID, id: UUID, /, **values: Any) -> int:
        allowed = {"summary", "summary_until_msg_id", "summary_version", "summary_updated_at"}
        allowed.add("metadata_")
        if not set(values) <= allowed:
            raise ValueError(f"memory may only update {sorted(allowed)}")
        return await super().update(app_id, id, **values)

    async def delete(self, app_id: UUID, id: UUID) -> int:
        raise TypeError("memory never deletes conversations")


class MessageReadRepository(AppScopedRepository[Message]):
    """Read-only view of messages for memory."""

    model = Message

    async def created_at_of(
        self, app_id: UUID, conversation_id: UUID, message_id: UUID
    ) -> datetime | None:
        stmt = select(Message.created_at).where(
            Message.app_id == app_id,
            Message.conversation_id == conversation_id,
            Message.id == message_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def non_system_after(
        self, app_id: UUID, conversation_id: UUID, after: tuple[datetime, UUID] | None, limit: int
    ) -> list[Message]:
        """Newest `limit` non-system messages after the (created_at, id) cursor, oldest first."""
        stmt = self._scoped(app_id).where(
            Message.conversation_id == conversation_id, Message.role != "system"
        )
        if after is not None:
            ts, mid = after
            stmt = stmt.where(
                (Message.created_at > ts) | ((Message.created_at == ts) & (Message.id > mid))
            )
        stmt = stmt.order_by(Message.created_at.desc(), Message.id.desc()).limit(limit)
        rows = list((await self.session.execute(stmt)).scalars())
        rows.reverse()
        return rows

    async def search(
        self,
        app_id: UUID,
        conversation_id: UUID,
        tsquery: str,
        exclude_ids: Sequence[UUID],
        k: int,
    ) -> list[tuple[Message, float]]:
        q = func.to_tsquery(literal_column("'simple'::regconfig"), tsquery)
        score = func.ts_rank_cd(Message.tsv, q).label("score")
        stmt = (
            select(Message, score)
            .where(
                Message.app_id == app_id,
                Message.conversation_id == conversation_id,
                Message.role != "system",
                Message.tsv.op("@@")(q),
            )
            .order_by(score.desc(), Message.created_at.desc())
            .limit(k)
        )
        if exclude_ids:
            stmt = stmt.where(Message.id.not_in(list(exclude_ids)))
        return [(m, float(s)) for m, s in (await self.session.execute(stmt)).all()]

    async def add(self, app_id: UUID, obj: Message) -> Message:
        raise TypeError("memory must not write messages (chat.service is the only writer)")

    async def update(self, app_id: UUID, id: UUID, /, **values: Any) -> int:
        raise TypeError("memory must not write messages (chat.service is the only writer)")

    async def delete(self, app_id: UUID, id: UUID) -> int:
        raise TypeError("memory must not write messages (chat.service is the only writer)")


class ConversationFactRepository(AppScopedRepository[ConversationFact]):
    model = ConversationFact

    async def get_by_key(
        self, app_id: UUID, conversation_id: UUID, key: str, *, for_update: bool = False
    ) -> ConversationFact | None:
        stmt = self._scoped(app_id).where(
            ConversationFact.conversation_id == conversation_id, ConversationFact.key == key
        )
        if for_update:
            stmt = stmt.with_for_update()
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_conversation(
        self, app_id: UUID, conversation_id: UUID, *, include_retracted: bool
    ) -> list[ConversationFact]:
        stmt = self._scoped(app_id).where(ConversationFact.conversation_id == conversation_id)
        if not include_retracted:
            stmt = stmt.where(ConversationFact.retracted_at.is_(None))
        stmt = stmt.order_by(ConversationFact.importance.desc(), ConversationFact.updated_at.desc())
        return list((await self.session.execute(stmt)).scalars())

    async def insert_new(
        self, app_id: UUID, conversation_id: UUID, values: dict[str, Any]
    ) -> ConversationFact | None:
        """INSERT … ON CONFLICT (conversation_id, key) DO NOTHING; None if the key exists."""
        stmt = (
            insert(ConversationFact)
            .values(app_id=app_id, conversation_id=conversation_id, **values)
            .on_conflict_do_nothing(index_elements=["conversation_id", "key"])
            .returning(ConversationFact)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def update_versioned(
        self, app_id: UUID, fact_id: UUID, expected_version: int, values: dict[str, Any]
    ) -> ConversationFact | None:
        """Compare-and-set on `version`; returns the new row or None on a lost race."""
        stmt = (
            update(ConversationFact)
            .where(
                ConversationFact.app_id == app_id,
                ConversationFact.id == fact_id,
                ConversationFact.version == expected_version,
            )
            .values(version=ConversationFact.version + 1, updated_at=func.now(), **values)
            .returning(ConversationFact)
            .execution_options(synchronize_session=False, populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def delete(self, app_id: UUID, id: UUID) -> int:
        raise TypeError("facts are never deleted (retract instead; cascade with the conversation)")


class ConversationFactHistoryRepository(AppScopedRepository[ConversationFactHistory]):
    """Append-only: the only write path is `append` (INSERT). update/delete are disabled here
    and rejected by a database trigger as well."""

    model = ConversationFactHistory

    async def append(self, app_id: UUID, row: ConversationFactHistory) -> ConversationFactHistory:
        return await self.add(app_id, row)

    async def list_for_conversation(
        self, app_id: UUID, conversation_id: UUID, *, key: str | None = None
    ) -> list[ConversationFactHistory]:
        stmt = self._scoped(app_id).where(
            ConversationFactHistory.conversation_id == conversation_id
        )
        if key is not None:
            stmt = stmt.where(ConversationFactHistory.key == key)
        stmt = stmt.order_by(ConversationFactHistory.changed_at, ConversationFactHistory.id)
        return list((await self.session.execute(stmt)).scalars())

    async def update(self, app_id: UUID, id: UUID, /, **values: Any) -> int:
        raise TypeError("conversation_fact_history is append-only")

    async def delete(self, app_id: UUID, id: UUID) -> int:
        raise TypeError("conversation_fact_history is append-only")


# ---------------------------------------------------------------- mapping
def _fact(row: ConversationFact) -> Fact:
    return Fact(
        id=row.id,
        key=row.key,
        value=row.value,
        category=row.category,
        importance=row.importance,
        confidence=row.confidence,
        source=row.source,
        version=row.version,
        source_message_id=row.source_message_id,
        first_seen_at=row.first_seen_at,
        updated_at=row.updated_at,
        retracted_at=row.retracted_at,
    )


def _change(row: ConversationFactHistory) -> FactChange:
    return FactChange(
        id=row.id,
        fact_id=row.fact_id,
        key=row.key,
        op=row.op,
        old_value=row.old_value,
        new_value=row.new_value,
        old_source_message_id=row.old_source_message_id,
        new_source_message_id=row.new_source_message_id,
        source=row.source,
        confidence=row.confidence,
        changed_at=row.changed_at,
    )


def _stored(m: Message) -> StoredMessage:
    return StoredMessage(
        id=m.id,
        role=m.role,
        content=m.content,
        token_count=m.token_count,
        created_at=m.created_at,
        answer_type=m.answer_type,
        interrupted=m.interrupted,
    )


# ---------------------------------------------------------------- store
class PgConversationStore:
    """ConversationStore on one AsyncSession; flushes but never commits."""

    def __init__(self, session: Any) -> None:
        self.session = session
        self.convs = ConversationSummaryRepository(session)
        self.messages = MessageReadRepository(session)
        self.facts = ConversationFactRepository(session)
        self.history = ConversationFactHistoryRepository(session)

    async def _require_conv(self, app_id: UUID, conversation_id: UUID) -> Conversation:
        conv = await self.convs.get(app_id, conversation_id)
        if conv is None:
            raise ConversationNotFoundError(details={"id": str(conversation_id)})
        return conv

    # ---- summary ----
    async def get_summary_state(self, app_id: UUID, conversation_id: UUID) -> SummaryState:
        c = await self._require_conv(app_id, conversation_id)
        summary = None if c.summary is None else ConversationSummary.model_validate(c.summary)
        return SummaryState(
            summary, c.summary_until_msg_id, c.summary_version, c.summary_updated_at
        )

    async def save_summary(
        self,
        app_id: UUID,
        conversation_id: UUID,
        summary: ConversationSummary,
        until_msg_id: UUID,
        expected_version: int,
    ) -> bool:
        conv = await self.convs.get_for_update(app_id, conversation_id)
        if conv is None:
            raise ConversationNotFoundError(details={"id": str(conversation_id)})
        if await self.messages.created_at_of(app_id, conversation_id, until_msg_id) is None:
            raise ValidationAppError(message="until_msg_id is not a message of this conversation")
        if conv.summary_version != expected_version:
            return False
        now = await self.session.scalar(select(func.now()))
        data = summary.model_dump()
        new_version = expected_version + 1
        meta = dict(conv.metadata_ or {})
        entry = {
            "version": new_version,
            "summary": data,
            "until_msg_id": str(until_msg_id),
            "updated_at": now.isoformat(),
        }
        meta["summary_history"] = [*meta.get("summary_history", []), entry][-SUMMARY_HISTORY_KEEP:]
        n = await self.convs.update(
            app_id,
            conversation_id,
            summary=data,
            summary_until_msg_id=until_msg_id,
            summary_version=new_version,
            summary_updated_at=now,
            metadata_=meta,
        )
        await self.session.refresh(conv)
        return n == 1

    # ---- messages ----
    async def turns_after(
        self, app_id: UUID, conversation_id: UUID, after_msg_id: UUID | None
    ) -> list[Turn]:
        await self._require_conv(app_id, conversation_id)
        cursor: tuple[datetime, UUID] | None = None
        if after_msg_id is not None:
            ts = await self.messages.created_at_of(app_id, conversation_id, after_msg_id)
            if ts is None:
                raise ValidationAppError(
                    message="after_msg_id is not a message of this conversation"
                )
            cursor = (ts, after_msg_id)
        rows = await self.messages.non_system_after(
            app_id, conversation_id, cursor, MAX_TURN_MESSAGES
        )
        return group_turns([_stored(m) for m in rows])

    async def search_messages(
        self,
        app_id: UUID,
        conversation_id: UUID,
        tsquery: str,
        exclude_ids: Sequence[UUID],
        k: int,
    ) -> list[Hit]:
        check_k(k)
        await self._require_conv(app_id, conversation_id)
        if not tsquery or not tsquery.strip():
            return []
        rows = await self.messages.search(app_id, conversation_id, tsquery, exclude_ids, k)
        return [Hit(m.id, m.role, m.content, m.created_at, s) for m, s in rows]

    # ---- facts ----
    async def list_facts(
        self, app_id: UUID, conversation_id: UUID, *, include_retracted: bool = False
    ) -> list[Fact]:
        await self._require_conv(app_id, conversation_id)
        rows = await self.facts.list_for_conversation(
            app_id, conversation_id, include_retracted=include_retracted
        )
        return [_fact(r) for r in rows]

    async def get_fact(self, app_id: UUID, conversation_id: UUID, key: str) -> Fact | None:
        await self._require_conv(app_id, conversation_id)
        row = await self.facts.get_by_key(app_id, conversation_id, key)
        return None if row is None else _fact(row)

    async def add_fact(self, app_id: UUID, conversation_id: UUID, data: FactWrite) -> Fact:
        await self._require_conv(app_id, conversation_id)
        row = await self.facts.insert_new(
            app_id,
            conversation_id,
            {
                "key": data.key,
                "value": data.value,
                "confidence": data.confidence,
                "source": data.source,
                "source_message_id": data.source_message_id,
                "category": fact_category(data.key),
                "importance": fact_importance(data.key),
            },
        )
        if row is None:
            raise FactExistsError(details={"key": data.key})
        await self.history.append(
            app_id,
            ConversationFactHistory(
                fact_id=row.id,
                app_id=app_id,
                conversation_id=conversation_id,
                key=data.key,
                op="add",
                new_value=data.value,
                new_source_message_id=data.source_message_id,
                source=data.source,
                confidence=data.confidence,
            ),
        )
        return _fact(row)

    async def _current(self, app_id: UUID, conversation_id: UUID, key: str) -> ConversationFact:
        await self._require_conv(app_id, conversation_id)
        row = await self.facts.get_by_key(app_id, conversation_id, key, for_update=True)
        if row is None:
            raise FactNotFoundError(details={"key": key})
        return row

    async def update_fact(
        self, app_id: UUID, conversation_id: UUID, data: FactWrite, *, expected_version: int
    ) -> Fact | None:
        current = await self._current(app_id, conversation_id, data.key)
        old = _fact(current)
        check_update(old, data)
        row = await self.facts.update_versioned(
            app_id,
            current.id,
            expected_version,
            {
                "value": data.value,
                "confidence": data.confidence,
                "source": data.source,
                "source_message_id": data.source_message_id,
                "retracted_at": None,
            },
        )
        if row is None:
            return None
        await self.history.append(
            app_id,
            ConversationFactHistory(
                fact_id=current.id,
                app_id=app_id,
                conversation_id=conversation_id,
                key=data.key,
                op="update",
                old_value=old.value,
                new_value=data.value,
                old_source_message_id=old.source_message_id,
                new_source_message_id=data.source_message_id,
                source=data.source,
                confidence=data.confidence,
            ),
        )
        return _fact(row)

    async def retract_fact(
        self,
        app_id: UUID,
        conversation_id: UUID,
        key: str,
        *,
        source: FactSource,
        confidence: float,
        source_message_id: UUID | None,
        expected_version: int,
    ) -> Fact | None:
        check_confidence(confidence)
        current = await self._current(app_id, conversation_id, key)
        old = _fact(current)
        if not old.active:
            return None
        row = await self.facts.update_versioned(
            app_id, current.id, expected_version, {"retracted_at": func.now()}
        )
        if row is None:
            return None
        await self.history.append(
            app_id,
            ConversationFactHistory(
                fact_id=current.id,
                app_id=app_id,
                conversation_id=conversation_id,
                key=key,
                op="retract",
                old_value=old.value,
                old_source_message_id=old.source_message_id,
                new_source_message_id=source_message_id,
                source=source,
                confidence=confidence,
            ),
        )
        return _fact(row)

    async def touch_fact(
        self, app_id: UUID, conversation_id: UUID, key: str, *, confidence: float
    ) -> Fact | None:
        check_confidence(confidence)
        await self._require_conv(app_id, conversation_id)
        stmt = (
            update(ConversationFact)
            .where(
                ConversationFact.app_id == app_id,
                ConversationFact.conversation_id == conversation_id,
                ConversationFact.key == key,
            )
            .values(
                confidence=func.greatest(ConversationFact.confidence, confidence),
                updated_at=func.now(),
            )
            .returning(ConversationFact)
            .execution_options(synchronize_session=False, populate_existing=True)
        )
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        return None if row is None else _fact(row)

    async def list_fact_history(
        self, app_id: UUID, conversation_id: UUID, *, key: str | None = None
    ) -> list[FactChange]:
        await self._require_conv(app_id, conversation_id)
        rows = await self.history.list_for_conversation(app_id, conversation_id, key=key)
        return [_change(r) for r in rows]
