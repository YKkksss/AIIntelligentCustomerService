"""M3.4 ②：坐席接口的业务规则（会话生命周期与转人工 §3.3）。

用 fake repository + fake `AppService` 跑真 `HandoffService`：分支（409 / 403 / 404、空文案不写
系统提示、抢单失败重读状态、交还备注）在这里覆盖；真实 SQL、并发抢单与扫描竞争由
`tests/integration/test_handoff_pg.py` 与 `test_isolation_pg.py` 覆盖。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from aics.core.errors import (
    ConversationNotFoundError,
    ForbiddenError,
    HandoffStateConflictError,
)
from aics.db.models.conversation import Conversation, Message
from aics.modules.apps.service import HandoffConfig
from aics.modules.chat.repository import ConversationClosedError
from aics.modules.handoff import service as handoff_service
from aics.modules.handoff.repository import OPEN_STATUSES
from aics.modules.handoff.service import HandoffService

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


def _conversation(app_id: UUID, **kw: Any) -> Conversation:
    fields: dict[str, Any] = {
        "id": uuid4(),
        "app_id": app_id,
        "channel": "api",
        "status": "active",
        "message_count": 4,
        "last_activity_at": NOW,
        "handoff_status": "pending",
        "handoff_reason": "keyword",
        "handoff_requested_at": NOW,
        "needs_callback": False,
        "handoff_agent_id": None,
        "handoff_agent_name": None,
        "handoff_accepted_at": None,
        "handoff_released_at": None,
        "handoff_end_reason": None,
        "external_user_id": "u_1",
        "refusal_streak": 0,
    }
    fields.update(kw)
    return Conversation(**fields)


class FakeRepo:
    """只实现 `HandoffService` 用到的方法，行为对齐真实 SQL 的语义。"""

    def __init__(self, conv: Conversation, *, changed: bool = True) -> None:
        self.conv = conv
        self.changed = changed
        self.calls: list[tuple[Any, ...]] = []

    async def get(self, app_id: UUID, conversation_id: UUID) -> Conversation | None:
        self.calls.append(("get", conversation_id))
        if app_id != self.conv.app_id or conversation_id != self.conv.id:
            return None
        return self.conv

    async def accept(
        self,
        app_id: UUID,
        conversation_id: UUID,
        *,
        agent_id: str,
        agent_name: str | None,
        at: datetime,
    ) -> int:
        self.calls.append(("accept", agent_id, agent_name))
        if not self.changed:
            self.conv.handoff_status = "accepted"  # 另一个坐席刚好抢先
            self.conv.handoff_agent_id = "a_999"
            return 0
        self.conv.handoff_status = "accepted"
        self.conv.handoff_agent_id = agent_id
        self.conv.handoff_agent_name = agent_name
        self.conv.handoff_accepted_at = at
        return 1

    async def release(
        self,
        app_id: UUID,
        conversation_id: UUID,
        *,
        expected: tuple[str, ...],
        end_reason: str,
        at: datetime,
    ) -> int:
        self.calls.append(("release", expected, end_reason))
        if not self.changed:
            self.conv.handoff_status = "released"  # 扫描任务或另一个坐席先释放
            return 0
        assert self.conv.handoff_status in expected
        self.conv.handoff_status = "released"
        self.conv.handoff_end_reason = end_reason
        self.conv.handoff_released_at = at
        self.conv.last_activity_at = at
        return 1

    async def status_of(self, app_id: UUID, conversation_id: UUID) -> str:
        return self.conv.handoff_status

    async def list_open(
        self,
        app_id: UUID,
        *,
        status: str,
        limit: int,
        after: UUID | None = None,
    ) -> tuple[list[Conversation], bool]:
        self.calls.append(("list_open", status, limit, after))
        rows = [self.conv] if self.conv.handoff_status == status else []
        return rows, False


class FakeSession:
    """只被 `commit()` 与 `refresh()` 触达。"""

    def __init__(self) -> None:
        self.commits = 0
        self.refreshed: list[Any] = []

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, instance: Any) -> None:
        # 真实 refresh 会用数据库里的值覆盖内存实例；fake 的 repo 已就地改过 conv
        self.refreshed.append(instance)


def _fake_apps(cfg: HandoffConfig) -> type:
    class FakeAppService:
        def __init__(self, session: Any) -> None:
            self.session = session

        @classmethod
        async def load(cls, session: Any, *, settings: Any = None) -> FakeAppService:
            return cls(session)

        async def get_config(self, app_id: UUID) -> Any:
            return SimpleNamespace(handoff=cfg)

    return FakeAppService


def _rig(
    monkeypatch: pytest.MonkeyPatch,
    conv: Conversation,
    *,
    cfg: HandoffConfig | None = None,
    changed: bool = True,
) -> tuple[HandoffService, FakeRepo, FakeSession, list[dict[str, Any]]]:
    appends: list[dict[str, Any]] = []

    async def fake_append(session: Any, app_id: UUID, conversation_id: UUID, **kw: Any) -> Message:
        appends.append({"app_id": app_id, "conversation_id": conversation_id, **kw})
        return Message(
            id=uuid4(),
            app_id=app_id,
            conversation_id=conversation_id,
            role=kw["role"],
            kind=kw.get("kind", "chat"),
            content=kw["content"],
            agent_id=kw.get("agent_id"),
        )

    monkeypatch.setattr(handoff_service, "append_agent_message", fake_append)
    monkeypatch.setattr(handoff_service, "AppService", _fake_apps(cfg or HandoffConfig()))
    repo = FakeRepo(conv, changed=changed)
    session = FakeSession()
    return HandoffService(session, repo=repo), repo, session, appends  # type: ignore[arg-type]


def test_state_reports_config_and_service_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    app_id = uuid4()
    conv = _conversation(app_id)
    svc, _, _, _ = _rig(monkeypatch, conv, cfg=HandoffConfig(handoff_ai_mode="wait"))

    state = asyncio.run(svc.state(app_id, conv.id))

    assert state.handoff_status == "pending"
    assert state.handoff_reason == "keyword"
    assert state.requested_at == NOW
    assert state.agent_id is None
    assert state.needs_callback is False
    assert state.handoff_ai_mode == "wait"
    assert state.in_service_hours is True  # service_hours 未配置 = 7×24


def test_state_allows_a_closed_conversation(monkeypatch: pytest.MonkeyPatch) -> None:
    """§3.1：会话关闭后 `handoff_status` 保留最后一个值，读接口不拒绝。"""
    app_id = uuid4()
    conv = _conversation(app_id, status="closed", handoff_status="released")
    svc, _, _, _ = _rig(monkeypatch, conv)

    state = asyncio.run(svc.state(app_id, conv.id))

    assert state.handoff_status == "released"


def test_state_unknown_conversation_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    app_id = uuid4()
    conv = _conversation(app_id)
    svc, _, _, _ = _rig(monkeypatch, conv)

    with pytest.raises(ConversationNotFoundError) as err:
        asyncio.run(svc.state(app_id, uuid4()))

    assert err.value.status == 404


def test_accept_writes_the_system_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    app_id = uuid4()
    conv = _conversation(app_id)
    svc, repo, session, appends = _rig(
        monkeypatch, conv, cfg=HandoffConfig(accepted_text="客服已接入")
    )

    state = asyncio.run(svc.accept(app_id, conv.id, agent_id="a_001", agent_name="小王"))

    assert state.handoff_status == "accepted"
    assert state.agent_id == "a_001"
    assert state.agent_name == "小王"
    assert state.accepted_at is not None
    assert session.commits == 1
    assert ("accept", "a_001", "小王") in repo.calls
    assert [a["kind"] for a in appends] == ["handoff_accepted"]
    assert appends[0]["role"] == "system"
    assert appends[0]["content"] == "客服已接入"


def test_accept_skips_the_notice_when_text_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    app_id = uuid4()
    conv = _conversation(app_id)
    svc, _, session, appends = _rig(monkeypatch, conv, cfg=HandoffConfig(accepted_text=None))

    asyncio.run(svc.accept(app_id, conv.id, agent_id="a_001"))

    assert appends == []
    assert session.commits == 1


def test_accept_conflict_reports_the_current_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """抢单输掉：条件更新 0 行 → 重读数据库里的状态 → 409 + details.handoff_status。"""
    app_id = uuid4()
    conv = _conversation(app_id)
    svc, _, session, appends = _rig(monkeypatch, conv, changed=False)

    with pytest.raises(HandoffStateConflictError) as err:
        asyncio.run(svc.accept(app_id, conv.id, agent_id="a_001"))

    assert err.value.status == 409
    assert err.value.details == {"handoff_status": "accepted"}
    assert session.commits == 0
    assert appends == []


def test_accept_on_closed_conversation_is_409(monkeypatch: pytest.MonkeyPatch) -> None:
    app_id = uuid4()
    conv = _conversation(app_id, status="closed")
    svc, repo, _, _ = _rig(monkeypatch, conv)

    with pytest.raises(ConversationClosedError) as err:
        asyncio.run(svc.accept(app_id, conv.id, agent_id="a_001"))

    assert err.value.status == 409
    assert repo.calls == [("get", conv.id)]  # 关闭的会话连 UPDATE 都不发


@pytest.mark.parametrize("status", ["none", "released"])
def test_agent_message_requires_pending_to_have_been_accepted(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    app_id = uuid4()
    conv = _conversation(app_id, handoff_status=status, handoff_agent_id="a_001")
    svc, _, _, appends = _rig(monkeypatch, conv)

    with pytest.raises(HandoffStateConflictError) as err:
        asyncio.run(svc.post_message(app_id, conv.id, agent_id="a_001", content="您好"))

    assert err.value.details == {"handoff_status": status}
    assert appends == []


def test_agent_message_requires_the_accepting_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    app_id = uuid4()
    conv = _conversation(app_id, handoff_status="accepted", handoff_agent_id="a_001")
    svc, _, _, appends = _rig(monkeypatch, conv)

    with pytest.raises(ForbiddenError) as err:
        asyncio.run(svc.post_message(app_id, conv.id, agent_id="a_002", content="您好"))

    assert err.value.status == 403
    assert err.value.details == {"reason": "not_the_accepting_agent"}
    assert appends == []


def test_agent_message_is_written_as_human_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    app_id = uuid4()
    conv = _conversation(app_id, handoff_status="accepted", handoff_agent_id="a_001")
    svc, _, session, appends = _rig(monkeypatch, conv)

    msg = asyncio.run(svc.post_message(app_id, conv.id, agent_id="a_001", content="我帮您查一下"))

    assert msg.role == "human_agent"
    assert msg.content == "我帮您查一下"
    assert session.commits == 1
    assert appends[0]["role"] == "human_agent"
    assert appends[0]["agent_id"] == "a_001"
    assert "kind" not in appends[0]  # 坐席消息是普通 chat 消息


def test_release_from_pending_is_allowed_for_any_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    app_id = uuid4()
    conv = _conversation(app_id, handoff_status="pending", handoff_agent_id=None)
    svc, repo, session, appends = _rig(
        monkeypatch, conv, cfg=HandoffConfig(released_text="已转回智能客服")
    )

    state = asyncio.run(svc.release(app_id, conv.id, agent_id="a_777", note="批量回退"))

    assert state.handoff_status == "released"
    assert state.handoff_end_reason == "agent"
    assert state.released_at is not None
    assert session.commits == 1
    assert ("release", OPEN_STATUSES, "agent") in repo.calls
    assert [a["kind"] for a in appends] == ["handoff_released"]


def test_release_from_accepted_requires_the_accepting_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_id = uuid4()
    conv = _conversation(app_id, handoff_status="accepted", handoff_agent_id="a_001")
    svc, repo, _, _ = _rig(monkeypatch, conv)

    with pytest.raises(ForbiddenError):
        asyncio.run(svc.release(app_id, conv.id, agent_id="a_002"))

    assert ("release", OPEN_STATUSES, "agent") not in repo.calls


@pytest.mark.parametrize("status", ["none", "released"])
def test_release_conflicts_when_nothing_is_open(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    app_id = uuid4()
    conv = _conversation(app_id, handoff_status=status)
    svc, repo, session, _ = _rig(monkeypatch, conv)

    with pytest.raises(HandoffStateConflictError) as err:
        asyncio.run(svc.release(app_id, conv.id, agent_id="a_001"))

    assert err.value.details == {"handoff_status": status}
    assert ("release", OPEN_STATUSES, "agent") not in repo.calls
    assert session.commits == 0


def test_release_lost_update_conflicts(monkeypatch: pytest.MonkeyPatch) -> None:
    """读到的还是 pending，但 UPDATE 落空（扫描任务刚释放）→ 409 + 重读后的状态。"""
    app_id = uuid4()
    conv = _conversation(app_id, handoff_status="pending")
    svc, _, session, appends = _rig(monkeypatch, conv, changed=False)

    with pytest.raises(HandoffStateConflictError) as err:
        asyncio.run(svc.release(app_id, conv.id, agent_id="a_001"))

    assert err.value.details == {"handoff_status": "released"}
    assert session.commits == 0
    assert appends == []


def test_list_open_maps_items(monkeypatch: pytest.MonkeyPatch) -> None:
    app_id = uuid4()
    conv = _conversation(app_id, handoff_reason="unanswerable", needs_callback=True)
    svc, repo, _, _ = _rig(monkeypatch, conv)

    items, more = asyncio.run(svc.list_open(app_id, status="pending", limit=5))

    assert more is False
    assert len(items) == 1
    item = items[0]
    assert item.conversation_id == conv.id
    assert item.handoff_status == "pending"
    assert item.handoff_reason == "unanswerable"
    assert item.needs_callback is True
    assert item.message_count == 4
    assert item.last_activity_at == NOW
    assert item.external_user_id == "u_1"
    assert ("list_open", "pending", 5, None) in repo.calls


def test_list_open_filters_by_status(monkeypatch: pytest.MonkeyPatch) -> None:
    app_id = uuid4()
    conv = _conversation(app_id, handoff_status="pending")
    svc, _, _, _ = _rig(monkeypatch, conv)

    items, _ = asyncio.run(svc.list_open(app_id, status="accepted"))

    assert items == []
