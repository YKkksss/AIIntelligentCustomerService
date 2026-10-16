"""M2.1: ConversationStore contract on the in-memory implementation + pure helpers."""

from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

import pytest

from aics.db.scoped import unscoped_methods
from aics.memory.repository import (
    ConversationFactHistoryRepository,
    ConversationFactRepository,
    ConversationSummaryRepository,
    MessageReadRepository,
)
from aics.memory.schemas import fact_category, fact_importance, is_valid_fact_key
from aics.memory.store import InMemoryConversationStore
from tests.memory_store_contract import CONTRACT, Harness


def _harness() -> Harness:
    store = InMemoryConversationStore()

    async def new_conversation(app_id: UUID) -> UUID:
        return store.create_conversation(app_id)

    async def add_message(app_id: UUID, cid: UUID, role: str, content: str, **kw: Any) -> UUID:
        kw.pop("kind", None)
        return store.add_message(app_id, cid, role, content, **kw).id

    return Harness(store, uuid.uuid4(), uuid.uuid4(), new_conversation, add_message)


@pytest.mark.parametrize("check", CONTRACT, ids=lambda f: f.__name__)
async def test_in_memory_contract(check: Any) -> None:
    await check(_harness())


async def test_summary_history_keeps_last_five() -> None:
    from aics.memory.schemas import ConversationSummary

    h = _harness()
    store: InMemoryConversationStore = h.store  # type: ignore[assignment]
    c = await h.new_conversation(h.app_a)
    m = await h.add_message(h.app_a, c, "user", "hi")
    for v in range(7):
        assert await store.save_summary(h.app_a, c, ConversationSummary(user_goal=str(v)), m, v)
    hist = store.summary_history(h.app_a, c)
    assert [e["version"] for e in hist] == [3, 4, 5, 6, 7]


@pytest.mark.parametrize(
    ("key", "category", "importance"),
    [
        ("order_no", "order", 3),
        ("phone", "identity", 3),
        ("address_region", "identity", 1),
        ("product", "product", 2),
        ("issue_type", "issue", 2),
        ("solution_given", "resolution", 1),
        ("user_emotion", "emotion", 1),
        ("other:delivery_slot", "other", 1),
    ],
)
def test_fact_key_derivation(key: str, category: str, importance: int) -> None:
    assert fact_category(key) == category and fact_importance(key) == importance


@pytest.mark.parametrize(
    "key", ["", "orderno", "other:", "other:UPPER", "other:" + "a" * 33, "phone "]
)
def test_invalid_fact_keys(key: str) -> None:
    assert not is_valid_fact_key(key)
    with pytest.raises(ValueError):
        fact_category(key)


def test_memory_repositories_scoped_and_write_paths_restricted() -> None:
    for cls in (
        ConversationSummaryRepository,
        MessageReadRepository,
        ConversationFactRepository,
        ConversationFactHistoryRepository,
    ):
        assert unscoped_methods(cls) == [], cls


async def test_history_repository_is_append_only() -> None:
    repo = ConversationFactHistoryRepository(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        await repo.update(uuid.uuid4(), uuid.uuid4(), new_value="x")
    with pytest.raises(TypeError):
        await repo.delete(uuid.uuid4(), uuid.uuid4())


async def test_memory_never_writes_messages() -> None:
    repo = MessageReadRepository(None)  # type: ignore[arg-type]
    for call in (
        repo.add(uuid.uuid4(), None),  # type: ignore[arg-type]
        repo.update(uuid.uuid4(), uuid.uuid4(), content="x"),
        repo.delete(uuid.uuid4(), uuid.uuid4()),
    ):
        with pytest.raises(TypeError):
            await call
