"""平台系统设置的读写与审计（M4.2 ④）。"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from aics.core.audit import AuditEntry
from aics.core.errors import ConfigVersionConflictError, ValidationAppError
from aics.modules.apps.admin_service import ActorMeta, AppsAdminService
from aics.modules.apps.system_settings import DEFAULT_SESSION_CONFIG_KEY, VERSION_KEY
from aics.rag.config import PLATFORM_DEFAULT_SESSION

Meta = ActorMeta(ip="10.0.0.1", user_agent="pytest", request_id="rid-1")


class FakeRow:
    def __init__(self, value: Any) -> None:
        self.value = value


class FakeSession:
    """`rows` 按 `system_settings.key` 取值；写操作只记录、不落库。"""

    def __init__(self, rows: dict[str, Any] | None = None) -> None:
        self.rows = dict(rows or {})
        self.executed: list[Any] = []
        self.added: list[Any] = []
        self.commits = 0

    async def get(self, _model: Any, key: Any) -> FakeRow | None:
        return None if key not in self.rows else FakeRow(self.rows[key])

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
    rows: dict[str, Any] | None = None,
) -> tuple[AppsAdminService, FakeSession, FakeAudit]:
    session = FakeSession(rows)
    audit = FakeAudit()
    return AppsAdminService(session, audit=audit), session, audit


async def test_reads_the_code_default_when_the_row_is_missing() -> None:
    service, _session, _audit = _service()

    body = await service.system_settings()

    assert body == {
        DEFAULT_SESSION_CONFIG_KEY: PLATFORM_DEFAULT_SESSION.model_dump(mode="json"),
        "version": 0,
    }


async def test_update_writes_audit_for_changed_keys_only() -> None:
    stored = PLATFORM_DEFAULT_SESSION.model_dump(mode="json")
    service, session, audit = _service({DEFAULT_SESSION_CONFIG_KEY: stored})

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
    service, session, audit = _service({DEFAULT_SESSION_CONFIG_KEY: stored})

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
    service, session, _audit = _service({DEFAULT_SESSION_CONFIG_KEY: stored})

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
    service, session, audit = _service({DEFAULT_SESSION_CONFIG_KEY: stored})

    body = await service.update_system_settings(
        {DEFAULT_SESSION_CONFIG_KEY: {"idle_close_minutes": 20}}, actor_id=uuid4(), meta=Meta
    )

    assert body[DEFAULT_SESSION_CONFIG_KEY] == {**stored, "idle_close_minutes": 20}
    assert session.commits == 1 and session.executed
    (entry,) = audit.entries
    assert entry.diff == {"before": {"idle_close_minutes": 10}, "after": {"idle_close_minutes": 20}}


async def test_version_is_reported_and_bumped_on_write() -> None:
    stored = PLATFORM_DEFAULT_SESSION.model_dump(mode="json")
    service, _session, _audit = _service({DEFAULT_SESSION_CONFIG_KEY: stored, VERSION_KEY: 3})

    before = await service.system_settings()
    after = await service.update_system_settings(
        {DEFAULT_SESSION_CONFIG_KEY: {"idle_close_minutes": 20}}, actor_id=uuid4()
    )

    assert before["version"] == 3
    assert after["version"] == 4


async def test_stale_expected_version_raises_conflict() -> None:
    stored = PLATFORM_DEFAULT_SESSION.model_dump(mode="json")
    service, session, audit = _service({DEFAULT_SESSION_CONFIG_KEY: stored, VERSION_KEY: 2})

    with pytest.raises(ConfigVersionConflictError) as err:
        await service.update_system_settings(
            {DEFAULT_SESSION_CONFIG_KEY: {"idle_close_minutes": 20}},
            actor_id=uuid4(),
            expected_version=1,
        )

    assert err.value.details == {"expected": 1, "current": 2}
    assert session.commits == 0
    assert audit.entries == []


async def test_matching_expected_version_goes_through() -> None:
    stored = PLATFORM_DEFAULT_SESSION.model_dump(mode="json")
    service, session, _audit = _service({DEFAULT_SESSION_CONFIG_KEY: stored, VERSION_KEY: 5})

    body = await service.update_system_settings(
        {DEFAULT_SESSION_CONFIG_KEY: {"idle_close_minutes": 20}},
        actor_id=uuid4(),
        expected_version=5,
    )

    assert body["version"] == 6
    assert body[DEFAULT_SESSION_CONFIG_KEY]["idle_close_minutes"] == 20
    assert session.commits == 1
