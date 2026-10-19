"""M3.6 ②：反馈服务的判定（`modules/feedback/service.py`）。

假 repository + 假 session：覆盖写、提交时机、两条拒绝（消息不存在、不是 assistant 消息）。
真实唯一键与 ON CONFLICT 的行为由 `tests/integration/test_feedback_pg.py` 覆盖。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from aics.core.errors import NotFoundError, ValidationAppError
from aics.modules.feedback.service import FeedbackService


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


class FakeMessages:
    def __init__(self, message: SimpleNamespace | None) -> None:
        self.message = message
        self.calls: list[tuple[UUID, UUID]] = []

    async def get(self, app_id: UUID, id: UUID) -> SimpleNamespace | None:
        self.calls.append((app_id, id))
        return self.message


class FakeFeedback:
    def __init__(self, row: SimpleNamespace | None = None) -> None:
        self.row = row
        self.calls: list[tuple[UUID, UUID, dict[str, Any]]] = []

    async def upsert(self, app_id: UUID, message_id: UUID, **values: Any) -> SimpleNamespace:
        self.calls.append((app_id, message_id, values))
        return self.row or SimpleNamespace(
            id=uuid4(), rating=values["rating"], message_id=message_id
        )


def _message(role: str = "assistant") -> SimpleNamespace:
    return SimpleNamespace(id=uuid4(), role=role, content="退货请在订单页申请")


def _service(
    *, message: SimpleNamespace | None, row: SimpleNamespace | None = None
) -> tuple[FeedbackService, FakeSession, FakeMessages, FakeFeedback]:
    session = FakeSession()
    messages = FakeMessages(message)
    feedback = FakeFeedback(row)
    service = FeedbackService(session, messages=messages, feedback=feedback)  # type: ignore[arg-type]
    return service, session, messages, feedback


async def test_submit_writes_and_commits() -> None:
    app_id, message = uuid4(), _message()
    service, session, messages, feedback = _service(message=message)

    row = await service.submit(app_id, message.id, rating=-1, comment="答非所问")

    assert row.rating == -1
    assert messages.calls == [(app_id, message.id)]
    assert feedback.calls == [(app_id, message.id, {"rating": -1, "comment": "答非所问"})]
    assert session.commits == 1


async def test_blank_comment_becomes_null() -> None:
    message = _message()
    service, _, _, feedback = _service(message=message)

    await service.submit(uuid4(), message.id, rating=1, comment="   ")

    assert feedback.calls[0][2] == {"rating": 1, "comment": None}


async def test_missing_message_is_404_with_the_id() -> None:
    message_id = uuid4()
    service, session, _, feedback = _service(message=None)

    with pytest.raises(NotFoundError) as err:
        await service.submit(uuid4(), message_id, rating=1)

    assert err.value.status == 404
    assert err.value.code == "NOT_FOUND"
    assert err.value.details == {"message_id": str(message_id)}
    assert feedback.calls == []  # 没有写，也没有提交
    assert session.commits == 0


@pytest.mark.parametrize("role", ["user", "human_agent", "system"])
async def test_only_assistant_messages_accept_feedback(role: str) -> None:
    message = _message(role)
    service, session, _, feedback = _service(message=message)

    with pytest.raises(ValidationAppError) as err:
        await service.submit(uuid4(), message.id, rating=1)

    assert err.value.status == 422
    assert err.value.details["errors"][0]["loc"] == ["path", "message_id"]
    assert feedback.calls == []
    assert session.commits == 0
