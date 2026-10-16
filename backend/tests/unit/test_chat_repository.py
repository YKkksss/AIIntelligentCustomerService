"""M2.1: message input validation, search_text rules and repository scoping."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from aics.db.scoped import unscoped_methods
from aics.modules.chat.repository import (
    ConversationRepository,
    MessageRepository,
    message_search_text,
)
from aics.modules.chat.schemas import MESSAGE_CONTENT_MAX_CHARS, NewConversation, NewMessage


def test_valid_messages() -> None:
    NewMessage(role="user", content="我的订单 A123 还没到")
    NewMessage(role="assistant", content="", answer_type="error")
    NewMessage(role="assistant", content="", answer_type="answer", interrupted=True)
    NewMessage(role="human_agent", content="您好", agent_id="agent-7")
    NewMessage(role="system", content="即将结束", kind="idle_notice")
    m = NewMessage(
        role="assistant",
        content="可以退货[1]",
        answer_type="answer",
        intent="kb_qa",
        intent_confidence=0.9,
        intent_source="llm",
        citations=[{"n": 1}],
        prompt_version="cc3.gen5.judge2",
    )
    assert m.kind == "chat"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"role": "robot", "content": "x"},
        {"role": "user", "content": "x", "kind": "idle_notice"},
        {"role": "system", "content": "x"},  # system needs a non-chat kind
        {"role": "system", "content": "x", "kind": "bogus"},
        {"role": "user", "content": "x", "answer_type": "answer"},
        {"role": "user", "content": "x", "handoff": True},
        {"role": "user", "content": "x", "prompt_version": "v1"},
        {"role": "user", "content": "x", "agent_id": "a"},
        {"role": "human_agent", "content": "x"},
        {"role": "assistant", "content": "x"},  # answer_type required
        {"role": "assistant", "content": "x", "answer_type": "answer", "intent_confidence": 2},
        {"role": "assistant", "content": "x", "answer_type": "answer", "intent": "weather"},
        {"role": "user", "content": "   "},
        {"role": "assistant", "content": "", "answer_type": "answer"},
        {"role": "user", "content": "x" * (MESSAGE_CONTENT_MAX_CHARS + 1)},
        {"role": "user", "content": "x", "token_count": 3},  # computed, never passed in
        {"role": "user", "content": "x", "search_text": "x"},
    ],
)
def test_invalid_messages(kwargs: dict) -> None:
    with pytest.raises(ValidationError):
        NewMessage(**kwargs)


def test_new_conversation_validation() -> None:
    NewConversation(channel="debug")
    with pytest.raises(ValidationError):
        NewConversation(channel="web")
    with pytest.raises(ValidationError):
        NewConversation(channel="api", external_user_id="u" * 129)


def test_search_text_rules() -> None:
    app = uuid.uuid4()
    st = message_search_text(app, "user", "我想申请退货退款", ())
    assert "退货" in st.split()
    assert message_search_text(app, "system", "如果没有其他问题，当前服务就结束咯~", ()) == ""
    assert message_search_text(app, "assistant", "", ()) == ""
    # custom dictionary words stay whole
    assert (
        "蓝牙耳机X2" in message_search_text(app, "user", "蓝牙耳机X2坏了", ("蓝牙耳机X2",)).split()
    )


def test_repositories_are_app_scoped() -> None:
    assert unscoped_methods(ConversationRepository) == []
    assert unscoped_methods(MessageRepository) == []


class _FakeSettingsRepo:
    def __init__(self, value: object) -> None:
        self.value = value

    async def get_value(self, key: str) -> object:
        return self.value


@pytest.mark.parametrize("stored", [None, {"idle_close_minutes": "bad"}])
async def test_default_session_falls_back_to_code_default(stored: object) -> None:
    from aics.modules.apps.system_settings import SystemSettingsService
    from aics.rag.config import PLATFORM_DEFAULT_SESSION

    svc = SystemSettingsService.__new__(SystemSettingsService)
    svc.repo = _FakeSettingsRepo(stored)  # type: ignore[assignment]
    assert await svc.get_default_session_config() == PLATFORM_DEFAULT_SESSION


async def test_unknown_system_setting_key_rejected() -> None:
    from aics.core.errors import ValidationAppError
    from aics.modules.apps.system_settings import SystemSettingsRepository

    with pytest.raises(ValidationAppError):
        await SystemSettingsRepository(None).set_value("nope", 1, updated_by=None)  # type: ignore[arg-type]
