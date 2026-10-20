"""M4.2 ②：`AppsAdminService` 的判定——乐观并发、审计 diff 与 `retokenize_app` 投递。

路由层只做参数解析，业务在这里；因此「版本冲突 409」「只记真正变化的字段」
「词典变了才建任务」都在这一层用假 session / 假 AppService 验证。
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from aics.core.audit import AuditEntry
from aics.core.errors import ConfigVersionConflictError
from aics.modules.apps.admin_service import ActorMeta, AppsAdminService


@dataclass
class FakeApp:
    id: UUID
    name: str = "旧名字"
    rag_config: dict[str, Any] | None = None
    handoff_config: dict[str, Any] | None = None
    session_config: dict[str, Any] | None = None
    chunk_config: dict[str, Any] | None = None
    webhook_config: dict[str, Any] | None = None
    custom_dict: list[str] | None = None
    config_version: int = 1
    chunk_config_version: int = 1
    status: str = "active"
    description: str | None = None
    greeting: str | None = None
    system_prompt: str | None = None
    answer_style: str = "concise"
    fallback_text: str = "抱歉"
    language: str = "zh-CN"
    created_at: Any = None
    updated_at: Any = None


class FakeSession:
    def __init__(self, *, documents: int = 0, chunks: int = 0, flagged: int = 0) -> None:
        self.commits = 0
        self.added: list[Any] = []
        self._counts = [documents, chunks, flagged]

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1

    async def scalar(self, _stmt: Any) -> int:
        return self._counts.pop(0) if self._counts else 0


class FakeAudit:
    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    async def write(self, _session: Any, entry: AuditEntry) -> None:
        self.entries.append(entry)


class FakeAppService:
    def __init__(self, app: FakeApp, *, custom_dict_changed: bool = False) -> None:
        self.app = app
        self.custom_dict_changed = custom_dict_changed
        self.patches: list[dict[str, Any]] = []

    async def get_app(self, _app_id: UUID) -> FakeApp:
        return self.app

    def config_of(self, _app: FakeApp) -> Any:
        return SimpleNamespace(
            effective_session=SimpleNamespace(model_dump=lambda **_: {"idle_close_minutes": 10}),
            webhook=SimpleNamespace(public=lambda: {"url": None, "secret_prefix": None}),
        )

    async def update_app(self, _app_id: UUID, patch: Any) -> Any:
        self.patches.append(dict(patch))
        if "name" in patch:
            self.app.name = patch["name"]
        if self.custom_dict_changed:
            self.app.custom_dict = ["新词"]
        return SimpleNamespace(
            app=self.app,
            config=SimpleNamespace(
                effective_session=SimpleNamespace(
                    model_dump=lambda **_: {"idle_close_minutes": 10}
                ),
                webhook=SimpleNamespace(public=lambda: {"url": None, "secret_prefix": None}),
            ),
            changed=True,
            reindex_required=False,
            custom_dict_changed=self.custom_dict_changed,
        )


def _service(
    *, audit: FakeAudit | None = None, enqueue: Any = None, custom_dict_changed: bool = False
) -> tuple[AppsAdminService, FakeSession, FakeApp, FakeAppService]:
    app = FakeApp(id=uuid4(), rag_config={}, handoff_config={}, session_config={}, chunk_config={})
    session = FakeSession()
    service = AppsAdminService(session, audit=audit, enqueue=enqueue)
    fake_apps = FakeAppService(app, custom_dict_changed=custom_dict_changed)
    service.apps = fake_apps  # type: ignore[assignment] - 假件替换真 AppService
    return service, session, app, fake_apps


async def test_update_rejects_a_stale_config_version() -> None:
    service, session, app, fake = _service()
    app.config_version = 5

    with pytest.raises(ConfigVersionConflictError) as err:
        await service.update(
            app.id, {"name": "新名字"}, expected_version=4, actor_id=uuid4(), meta=Meta
        )

    assert err.value.details == {"expected": 4, "current": 5}
    assert fake.patches == [] and session.commits == 0


Meta = ActorMeta(ip="10.0.0.1", user_agent="pytest", request_id="rid-1")


async def test_update_writes_audit_only_for_changed_fields() -> None:
    audit = FakeAudit()
    service, session, app, _fake = _service(audit=audit)

    await service.update(
        app.id, {"name": "新名字"}, expected_version=1, actor_id=uuid4(), meta=Meta
    )

    assert session.commits == 1
    assert len(audit.entries) == 1
    entry = audit.entries[0]
    assert entry.action == "app.update"
    assert entry.diff == {"before": {"name": "旧名字"}, "after": {"name": "新名字"}}
    assert entry.ip == "10.0.0.1" and entry.request_id == "rid-1"


async def test_update_skips_audit_when_nothing_changed() -> None:
    audit = FakeAudit()
    service, _session, app, _fake = _service(audit=audit)

    await service.update(
        app.id, {}, expected_version=app.config_version, actor_id=uuid4(), meta=Meta
    )

    assert audit.entries == []


async def test_custom_dict_change_creates_and_dispatches_the_retokenize_job() -> None:
    calls: list[tuple[str, tuple[str, ...]]] = []
    service, session, app, _fake = _service(
        enqueue=lambda task, args: calls.append((task, tuple(args))), custom_dict_changed=True
    )

    body = await service.update(
        app.id, {"custom_dict": ["新词"]}, expected_version=1, actor_id=uuid4(), meta=Meta
    )

    job = session.added[0]
    assert job.type == "retokenize_app" and job.app_id == app.id
    assert body["retokenize_job_id"] == str(job.id)
    assert calls == [("aics.retokenize_app", (str(app.id), str(job.id)))]


async def test_detail_merges_config_and_stats() -> None:
    service, _session, app, _fake = _service()
    app.rag_config = {"vector_top_k": 20}

    body = await service.detail(app.id)

    assert body["name"] == app.name
    assert body["rag_config"] == {"vector_top_k": 20}
    assert body["effective_session_config"] == {"idle_close_minutes": 10}
    assert body["stats"] == {"documents": 0, "chunks": 0, "flagged_chunks": 0}
