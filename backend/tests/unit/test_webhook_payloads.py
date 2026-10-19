"""`webhooks.payloads`：固定格式与八种事件 data 的单元测试（M3.5 ①，接入指南 §7.2–7.3）。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from aics.modules.webhooks import payloads

NOW = datetime(2026, 10, 19, 4, 30, tzinfo=UTC)
EVENT_ID = uuid.uuid4()
APP = uuid.uuid4()
CONV = uuid.uuid4()


def _message(**kw: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "role": "human_agent",
        "kind": "chat",
        "content": "在的，请讲",
        "agent_id": "ag_1",
        "answer_type": None,
        "created_at": NOW,
    }
    return SimpleNamespace(**{**base, **kw})


def test_envelope_has_every_public_field() -> None:
    payload = payloads.build_payload(
        delivery_id=EVENT_ID,
        event="handoff.requested",
        app_id=APP,
        conversation_id=CONV,
        user_id="u_10086",
        seq=7,
        occurred_at=NOW,
        data={"reason": "intent_human"},
    )
    assert payload == {
        "id": str(EVENT_ID),
        "event": "handoff.requested",
        "app_id": str(APP),
        "conversation_id": str(CONV),
        "user_id": "u_10086",
        "seq": 7,
        "occurred_at": "2026-10-19T04:30:00+00:00",
        "data": {"reason": "intent_human"},
    }


def test_envelope_tolerates_a_missing_conversation_and_user() -> None:
    payload = payloads.build_payload(
        delivery_id=EVENT_ID,
        event="conversation.closed",
        app_id=APP,
        conversation_id=None,
        user_id=None,
        seq=1,
        occurred_at=NOW,
        data={"reason": "idle"},
    )
    assert payload["conversation_id"] is None and payload["user_id"] is None


def test_naive_timestamps_are_read_as_utc() -> None:
    assert payloads.iso(datetime(2026, 10, 19, 4, 30)) == "2026-10-19T04:30:00+00:00"


def test_message_dict_carries_the_agent_fields() -> None:
    msg = _message()
    assert payloads.message_dict(msg) == {
        "id": str(msg.id),
        "role": "human_agent",
        "kind": "chat",
        "content": "在的，请讲",
        "agent_id": "ag_1",
        "answer_type": None,
        "created_at": "2026-10-19T04:30:00+00:00",
    }
    assert payloads.message_created(msg) == {"message": payloads.message_dict(msg)}


def test_recent_messages_are_oldest_first_without_answer_metadata() -> None:
    rows = [_message(role="user", agent_id=None), _message()]
    assert [m["role"] for m in payloads.recent_messages(rows)] == ["user", "human_agent"]
    assert set(payloads.recent_messages(rows)[0]) == {"id", "role", "kind", "content", "created_at"}


def test_summary_text_renders_the_rolling_summary() -> None:
    assert payloads.summary_text(None) is None
    assert payloads.summary_text({}) is None
    assert (
        payloads.summary_text(
            {
                "user_goal": "退货",
                "confirmed_info": ["订单 123"],
                "solutions_given": [],
                "open_issues": ["运费未定"],
            }
        )
        == "退货；已确认：订单 123；待解决：运费未定"
    )


def test_handoff_requested_shape() -> None:
    data = payloads.handoff_requested(
        reason="intent_human",
        intent="human",
        handoff_ai_mode="wait",
        summary="退货",
        facts={"order_no": "1"},
        recent=[_message(role="user", agent_id=None)],
    )
    assert data["reason"] == "intent_human"
    assert data["intent"] == "human"
    assert data["handoff_ai_mode"] == "wait"
    assert data["summary"] == "退货"
    assert data["facts"] == {"order_no": "1"}
    assert len(data["recent_messages"]) == 1


def test_handoff_offline_shape() -> None:
    msg = _message(role="system", kind="handoff_offline", agent_id=None, content="非服务时间")
    data = payloads.handoff_offline(
        message=msg,
        reason="keyword",
        needs_callback=True,
        summary=None,
        facts=None,
        recent=[],
    )
    assert data["message"]["kind"] == "handoff_offline"
    assert (data["reason"], data["needs_callback"], data["facts"], data["recent_messages"]) == (
        "keyword",
        True,
        {},
        [],
    )


def test_handoff_accepted_and_released_shapes() -> None:
    assert payloads.handoff_accepted(agent_id="ag_1", agent_name=None) == {
        "agent_id": "ag_1",
        "agent_name": None,
    }
    assert payloads.handoff_released(
        agent_id=None, note=None, released_by="system", reason="accepted_idle"
    ) == {"agent_id": None, "note": None, "released_by": "system", "reason": "accepted_idle"}


def test_queue_timeout_idle_notice_and_closed_shapes() -> None:
    msg = _message(role="system", kind="handoff_queue_timeout", agent_id=None)
    assert payloads.handoff_queue_timeout(message=msg, waited_seconds=300, needs_callback=True) == {
        "message": payloads.message_dict(msg),
        "waited_seconds": 300,
        "needs_callback": True,
    }
    assert payloads.idle_notice(message=msg, close_at=NOW) == {
        "message": payloads.message_dict(msg),
        "close_at": "2026-10-19T04:30:00+00:00",
    }
    assert payloads.conversation_closed(reason="handoff_idle") == {"reason": "handoff_idle"}
