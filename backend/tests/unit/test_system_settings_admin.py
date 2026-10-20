"""平台系统设置的读写与审计（M4.2 ④）。"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from aics.core.audit import AuditEntry
from aics.core.errors import ValidationAppError
from aics.modules.apps.admin_service import ActorMeta, AppsAdminService
from aics.modules.apps.system_settings import DEFAULT_SESSION_CONFIG_KEY
from aics.rag.config import PLATFORM_DEFAULT_SESSION

Meta = ActorMeta(ip="10.0.0.1", user_agent="pytest", request_id="rid-1")


class FakeRow:
    def __init__(self, value: Any) -> None:
        self.value = value


class FakeSession:
    def __init__(self, value: dict[str, Any] | None = None) -> None:
        self.value = value
        self.executed: list[Any] = []
        self.added: list[Any] = []
        self.commits = 0

    async def get(self, _model: Any, _key: Any) -> FakeRow | None:
        return None if self.value is None else FakeRow(self.value)

    async def execute(self, stmt: Any) -> None:
        self.executed.append(stmt)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1


class FakeAudit:
    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    async def write(self, _session: Any, entry: AuditEntry) -> None:
        self.entries.append(entry)


def _service(
    value: dict[str, Any] | None = None,
) -> tuple[AppsAdminService, FakeSession, FakeAudit]:
    session = FakeSession(value)
    audit = FakeAudit()
    return AppsAdminService(session, audit=audit), session, audit


async def test_reads_the_code_default_when_the_row_is_missing() -> None:
    service, _session, _audit = _service()

    body = await service.system_settings()

    assert body == {DEFAULT_SESSION_CONFIG_KEY: PLATFORM_DEFAULT_SESSION.model_dump(mode="json")}


async def test_update_writes_audit_for_changed_keys_only() -> None:
    stored = PLATFORM_DEFAULT_SESSION.model_dump(mode="json")
    service, session, audit = _service(stored)

    body = await service.update_system_settings(
        {DEFAULT_SESSION_CONFIG_KEY: {**stored, "idle_close_minutes": 20}},
        actor_id=uuid4(),
        meta=Meta,
    )

    assert body[DEFAULT_SESSION_CONFIG_KEY]["idle_close_minutes"] == 20
    assert session.commits == 1 and session.executed
    (entry,) = audit.entries
    assert entry.action == "system.update"
    assert entry.app_id is None and entry.target_type == "system" and entry.target_id is None
    assert entry.diff == {"before": {"idle_close_minutes": 10}, "after": {"idle_close_minutes": 20}}
    assert entry.ip == "10.0.0.1" and entry.request_id == "rid-1"


async def test_update_skips_audit_when_nothing_changed() -> None:
    stored = PLATFORM_DEFAULT_SESSION.model_dump(mode="json")
    service, session, audit = _service(stored)

    await service.update_system_settings(
        {DEFAULT_SESSION_CONFIG_KEY: dict(stored)}, actor_id=uuid4(), meta=Meta
    )

    assert audit.entries == [] and session.commits == 1


async def test_update_rejects_a_payload_that_is_not_an_object() -> None:
    service, session, _audit = _service()

    with pytest.raises(ValidationAppError) as err:
        await service.update_system_settings(
            {DEFAULT_SESSION_CONFIG_KEY: 3}, actor_id=uuid4(), meta=Meta
        )

    assert err.value.details["errors"][0]["loc"] == [DEFAULT_SESSION_CONFIG_KEY]
    assert session.executed == [] and session.commits == 0


async def test_update_reports_the_invalid_field_path() -> None:
    stored = PLATFORM_DEFAULT_SESSION.model_dump(mode="json")
    service, session, _audit = _service(stored)

    with pytest.raises(ValidationAppError) as err:
        await service.update_system_settings(
            {DEFAULT_SESSION_CONFIG_KEY: {**stored, "idle_close_minutes": 0}},
            actor_id=uuid4(),
            meta=Meta,
        )

    errors = err.value.details["errors"]
    assert errors[0]["loc"] == [DEFAULT_SESSION_CONFIG_KEY, "idle_close_minutes"]


async def test_partial_patch_merges_with_the_current_values() -> None:
    """PATCH 只给部分字段也要成立（M4.2 ⑤ 验收脚本发现的 422 回归）。"""
    stored = PLATFORM_DEFAULT_SESSION.model_dump(mode="json")
    service, session, audit = _service(stored)

    body = await service.update_system_settings(
        {DEFAULT_SESSION_CONFIG_KEY: {"idle_close_minutes": 20}}, actor_id=uuid4(), meta=Meta
    )

    assert body[DEFAULT_SESSION_CONFIG_KEY] == {**stored, "idle_close_minutes": 20}
    assert session.commits == 1 and session.executed
    (entry,) = audit.entries
    assert entry.diff == {"before": {"idle_close_minutes": 10}, "after": {"idle_close_minutes": 20}}
