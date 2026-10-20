"""M4.5 ②：Key 管理服务——脱敏视图、明文只出现一次、审计与 404。

路由层的参数解析在 `test_apikeys_admin_api.py`；这里只测业务规则本身。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from aics.core.errors import NotFoundError, ValidationAppError
from aics.modules.apikeys.admin_service import ApiKeyAdminService, key_row

APP_ID = uuid4()
ACTOR = uuid4()
META = SimpleNamespace(ip="127.0.0.1", user_agent="pytest", request_id="req-1")


def make_row(**over: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": uuid4(),
        "name": "生产接入",
        "prefix": "aics_abcdefg",
        "scopes": ["chat", "kb:read"],
        "rpm_limit": 60,
        "daily_quota": None,
        "ip_allowlist": None,
        "last_used_at": None,
        "expires_at": None,
        "revoked_at": None,
        "created_at": datetime.now(UTC),
    }
    base.update(over)
    return SimpleNamespace(**base)


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.refreshed: list[Any] = []

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, row: Any) -> None:
        self.refreshed.append(row)


class FakeRepo:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[tuple[Any, ...]] = []

    async def list_for_app(self, app_id: Any) -> list[Any]:
        self.calls.append(("list_for_app", app_id))
        return self.rows

    async def get(self, app_id: Any, key_id: Any) -> Any:
        self.calls.append(("get", app_id, key_id))
        return next((r for r in self.rows if r.id == key_id), None)


class FakeKeys:
    def __init__(self, *, created: Any = None, ok: bool = True) -> None:
        self.created = created or make_row()
        self.ok = ok
        self.calls: list[tuple[Any, ...]] = []

    async def create(self, app_id: Any, **kwargs: Any) -> tuple[Any, str]:
        self.calls.append(("create", app_id, kwargs))
        return self.created, "aics_SECRETSECRETSECRETSECRETSECRET12"

    async def update_limits(self, app_id: Any, key_id: Any, **kwargs: Any) -> bool:
        self.calls.append(("update_limits", app_id, key_id, kwargs))
        return self.ok

    async def set_ip_allowlist(self, app_id: Any, key_id: Any, cidrs: Any) -> bool:
        self.calls.append(("set_ip_allowlist", app_id, key_id, cidrs))
        return self.ok

    async def revoke(self, app_id: Any, key_id: Any) -> bool:
        self.calls.append(("revoke", app_id, key_id))
        return self.ok


class FakeAudit:
    def __init__(self) -> None:
        self.entries: list[Any] = []

    async def write(self, session: Any, entry: Any) -> None:
        self.entries.append(entry)


def build(
    *, rows: list[Any] | None = None, keys: FakeKeys | None = None, audit: FakeAudit | None = None
) -> tuple[ApiKeyAdminService, FakeSession, FakeRepo, FakeKeys, FakeAudit]:
    session = FakeSession()
    sink = audit or FakeAudit()
    svc = ApiKeyAdminService(session, pepper="pepper", audit=sink)
    repo = FakeRepo(rows or [])
    fake_keys = keys or FakeKeys()
    svc.repo = repo  # type: ignore[assignment]
    svc.keys = fake_keys  # type: ignore[assignment]
    return svc, session, repo, fake_keys, sink


# --- 视图 ---------------------------------------------------------------


def test_key_row_never_carries_the_secret() -> None:
    row = make_row()
    body = key_row(row)
    assert "key_hash" not in body
    assert "key" not in body
    assert body["status"] == "active"
    assert body["hint"].endswith(row.prefix[-4:])
    assert body["scopes"] == ["chat", "kb:read"]


def test_key_row_status_expired_and_revoked() -> None:
    past = datetime.now(UTC) - timedelta(minutes=1)
    assert key_row(make_row(expires_at=past))["status"] == "expired"
    # 吊销优先于过期
    assert key_row(make_row(expires_at=past, revoked_at=past))["status"] == "revoked"


async def test_list_keys_uses_the_masked_view() -> None:
    row = make_row()
    svc, _session, repo, _keys, _audit = build(rows=[row])
    items = await svc.list_keys(APP_ID)
    assert [item["id"] for item in items] == [row.id]
    assert repo.calls == [("list_for_app", APP_ID)]
    assert "key" not in items[0]


# --- 创建 ---------------------------------------------------------------


async def test_create_returns_plaintext_once_and_audits() -> None:
    svc, session, _repo, keys, audit = build()
    body = await svc.create_key(
        APP_ID, name="生产接入", scopes=["chat"], rpm_limit=30, actor_id=ACTOR, meta=META
    )
    assert body["key"] == "aics_SECRETSECRETSECRETSECRETSECRET12"
    assert session.commits == 1
    assert keys.calls[0][2]["created_by"] == ACTOR
    assert keys.calls[0][2]["rpm_limit"] == 30
    entry = audit.entries[0]
    assert entry.action == "key.create"
    assert entry.target_type == "api_key"
    assert entry.app_id == APP_ID
    assert entry.ip == "127.0.0.1"
    assert "key" not in entry.diff["after"]  # 明文不进审计


async def test_create_without_actor_skips_the_audit() -> None:
    svc, _session, _repo, _keys, audit = build()
    await svc.create_key(APP_ID, name="无名", scopes=["chat"])
    assert audit.entries == []


# --- 修改 ---------------------------------------------------------------


async def test_update_writes_limits_but_skips_commit_when_nothing_changed() -> None:
    row = make_row()
    svc, session, _repo, keys, audit = build(rows=[row])
    await svc.update_key(APP_ID, row.id, rpm_limit=60, actor_id=ACTOR)
    assert keys.calls[0][3] == {"rpm_limit": 60, "daily_quota": None}
    assert session.commits == 0  # 视图未变 → 不提交
    assert audit.entries == []


async def test_update_keeps_the_quota_when_only_rpm_is_given() -> None:
    row = make_row(daily_quota=100)
    svc, _session, _repo, keys, _audit = build(rows=[row])
    await svc.update_key(APP_ID, row.id, rpm_limit=5)
    assert keys.calls[0][3] == {"rpm_limit": 5, "daily_quota": 100}


async def test_update_clears_the_quota_when_null_is_given() -> None:
    row = make_row(daily_quota=100)
    svc, _session, _repo, keys, _audit = build(rows=[row])
    await svc.update_key(APP_ID, row.id, daily_quota=None)
    assert keys.calls[0][3] == {"rpm_limit": 60, "daily_quota": None}


async def test_update_never_touches_the_allowlist_when_omitted() -> None:
    row = make_row()
    svc, _session, _repo, keys, _audit = build(rows=[row])
    await svc.update_key(APP_ID, row.id, daily_quota=10)
    assert [c[0] for c in keys.calls] == ["update_limits"]


async def test_update_sets_the_allowlist() -> None:
    row = make_row()
    svc, _session, _repo, keys, _audit = build(rows=[row])
    await svc.update_key(APP_ID, row.id, ip_allowlist=["10.0.0.0/8"])
    assert keys.calls[-1] == ("set_ip_allowlist", APP_ID, row.id, ["10.0.0.0/8"])


async def test_update_raises_404_for_an_unknown_key() -> None:
    svc, _session, _repo, _keys, _audit = build(rows=[])
    missing = uuid4()
    with pytest.raises(NotFoundError) as info:
        await svc.update_key(APP_ID, missing, rpm_limit=10)
    assert info.value.details == {"key_id": str(missing)}


async def test_update_raises_404_when_the_repo_misses_the_row() -> None:
    row = make_row()
    svc, _session, _repo, _keys, _audit = build(rows=[row], keys=FakeKeys(ok=False))
    with pytest.raises(NotFoundError):
        await svc.update_key(APP_ID, row.id, rpm_limit=10)


async def test_update_bubbles_validation_errors() -> None:
    row = make_row()
    session = FakeSession()
    svc = ApiKeyAdminService(session, pepper="pepper")
    svc.repo = FakeRepo([row])  # type: ignore[assignment]
    svc.keys = FakeKeys()  # type: ignore[assignment]

    async def reject(*_args: Any, **_kwargs: Any) -> bool:
        raise ValidationAppError(details={"errors": [{"loc": ["rpm_limit"]}]})

    svc.keys.update_limits = reject  # type: ignore[method-assign]
    with pytest.raises(ValidationAppError):
        await svc.update_key(APP_ID, row.id, rpm_limit=0)


# --- 吊销与示例 ---------------------------------------------------------


async def test_revoke_returns_the_revoked_row_and_audits() -> None:
    row = make_row(revoked_at=datetime.now(UTC))
    audit = FakeAudit()
    svc, session, _repo, keys, _ = build(rows=[row], audit=audit)
    body = await svc.revoke_key(APP_ID, row.id, actor_id=ACTOR)
    assert body["status"] == "revoked"
    assert keys.calls == [("revoke", APP_ID, row.id)]
    assert audit.entries[0].action == "key.revoke"
    assert session.commits == 1


async def test_revoke_404_for_an_unknown_key() -> None:
    svc, _session, _repo, _keys, _audit = build(rows=[], keys=FakeKeys(ok=False))
    with pytest.raises(NotFoundError):
        await svc.revoke_key(APP_ID, uuid4())


async def test_example_uses_a_placeholder_and_the_app_scoped_url() -> None:
    row = make_row()
    svc, _session, _repo, _keys, _audit = build(rows=[row])
    body = await svc.example(APP_ID, row.id, base_url="https://aics.example.com")
    assert "<YOUR_API_KEY>" in body["curl"]
    assert "<YOUR_API_KEY>" in body["python"]
    assert "<YOUR_API_KEY>" in body["javascript"]
    assert f"/api/v1/apps/{APP_ID}" in body["url"]
    assert "aics_abcdefg" not in body["curl"]  # 连前缀都不拼进示例
