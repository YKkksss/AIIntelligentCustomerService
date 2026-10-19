"""M2.7: lifecycle policy resolution and message schema (DB paths: integration tests)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from aics.modules.chat.lifecycle import SweepResult, policy_of
from aics.modules.chat.schemas import NewMessage
from aics.rag.config import PLATFORM_DEFAULT_SESSION


def app(session: object = None, handoff: object = None) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), session_config=session, handoff_config=handoff)


def test_policy_defaults_to_platform() -> None:
    p = policy_of(app(), PLATFORM_DEFAULT_SESSION)
    assert p.session == PLATFORM_DEFAULT_SESSION and p.pause_pending is False


def test_policy_merges_app_values_and_wait_mode() -> None:
    p = policy_of(
        app({"idle_close_minutes": 30}, {"handoff_ai_mode": "wait"}), PLATFORM_DEFAULT_SESSION
    )
    assert p.session.idle_close_minutes == 30
    assert (
        p.session.idle_notice_before_minutes == PLATFORM_DEFAULT_SESSION.idle_notice_before_minutes
    )
    assert p.pause_pending is True


def test_policy_tolerates_invalid_stored_config() -> None:
    p = policy_of(
        app({"idle_close_minutes": 0}, {"handoff_ai_mode": "bogus"}), PLATFORM_DEFAULT_SESSION
    )
    assert p.session == PLATFORM_DEFAULT_SESSION and p.pause_pending is False
    # merged result invalid (notice ≥ close) → platform default as a whole
    p = policy_of(app({"idle_close_minutes": 1}), PLATFORM_DEFAULT_SESSION)
    assert p.session == PLATFORM_DEFAULT_SESSION


def test_sweep_result_dict() -> None:
    r = SweepResult(apps=2, noticed=1, closed=3, skipped_apps=["x"])
    assert r.as_dict() == {"apps": 2, "noticed": 1, "closed": 3, "skipped_apps": 1}


def test_idle_closed_marker_may_be_empty_but_notice_may_not() -> None:
    assert NewMessage(role="system", kind="idle_closed", content="").content == ""
    # §3.8：接入后无消息直接关会话也留一个空内容的时间线标记（M3.4 ③ 起允许）
    assert NewMessage(role="system", kind="handoff_idle_closed", content="").content == ""
    with pytest.raises(ValidationError):
        NewMessage(role="system", kind="idle_notice", content=" ")
    with pytest.raises(ValidationError):
        NewMessage(role="system", kind="handoff_offline", content=" ")
