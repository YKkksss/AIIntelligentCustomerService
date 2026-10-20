"""M4.2 ③：应用的创建、复制与删除（`AppsAdminService`）。

删除是长任务：接口只负责置 `deleting`、吊销 Key、关闭会话与建 `cleanup_app` 任务，
真正的数据清理在 `test_cleanup_app_pg.py`（集成，真 PG）与 `test_cleanup_app.py`（单测，假件）。
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from aics.core.audit import AuditEntry
from aics.core.errors import ValidationAppError
from aics.modules.apps.admin_service import ActorMeta, AppsAdminService

META = ActorMeta(ip="10.0.0.1", user_agent="pytest", request_id="rid-1")


@dataclass
class FakeApp:
    id: UUID
    name: str = "售后客服"
    status: str = "active"
    description: str | None = "描述"
    greeting: str | None = "你好"
    system_prompt: str | None = "你是客服"
    answer_style: str = "concise"
    fallback_text: str = "抱歉"
    language: str = "zh-CN"
    rag_config: dict[str, Any] | None = None
    handoff_config: dict[str, Any] | None = None
    session_config: dict[str, Any] | None = None
    chunk_config: dict[str, Any] | None = None
    webhook_config: dict[str, Any] | None = None
    custom_dict: list[str] | None = None
    config_version: int = 1
    chunk_config_version: int = 1
    created_at: Any = None
    updated_at: Any = None


class _Result:
    def __init__(self, values: list[Any] | None = None, rowcount: int = 0) -> None:
        self._values = values or []
        self.rowcount = rowcount

    def scalars(self) -> list[Any]:
        return list(self._values)


class FakeSession:
    def __init__(self, results: list[_Result] | None = None) -> None:
        self.results = list(results or [])
        self.commits = 0
        self.added: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1

    async def execute(self, _stmt: Any) -> _Result:
        return self.results.pop(0) if self.results else _Result()

    async def scalar(self, _stmt: Any) -> int:
        return 0


class FakeAudit:
    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    async def write(self, _session: Any, entry: AuditEntry) -> None:
        self.entries.append(entry)


class FakeAppService:
    def __init__(self, app: FakeApp, *, created: FakeApp | None = None) -> None:
        self.app = app
        self.created = created or FakeApp(id=uuid4(), name="新建")
        self.created_kwargs: dict[str, Any] = {}

    async def get_app(self, app_id: UUID) -> FakeApp:
        return self.created if app_id == self.created.id else self.app

    def config_of(self, _app: FakeApp) -> Any:
        return SimpleNamespace(
            effective_session=SimpleNamespace(model_dump=lambda **_: {"idle_close_minutes": 10}),
            webhook=SimpleNamespace(public=lambda: {"url": None, "secret_prefix": None}),
        )

    async def create_app(self, **kwargs: Any) -> FakeApp:
        self.created_kwargs = kwargs
        return self.created

    async def list_apps(self, **_kwargs: Any) -> list[FakeApp]:
        return [self.created]


def _service(
    *,
    results: list[_Result] | None = None,
    audit: FakeAudit | None = None,
    enqueue: Any = None,
    redis: Any = None,
    created: FakeApp | None = None,
) -> tuple[AppsAdminService, FakeSession, FakeApp, FakeAppService]:
    app = FakeApp(id=uuid4(), rag_config={}, handoff_config={}, chunk_config={})
    session = FakeSession(results)
    service = AppsAdminService(session, audit=audit, enqueue=enqueue, redis=redis)
    fake = FakeAppService(app, created=created)
    service.apps = fake  # type: ignore[assignment] - 假件替换真 AppService
    return service, session, app, fake


async def test_create_requires_a_name() -> None:
    service, _session, _app, fake = _service()

    with pytest.raises(ValidationAppError) as err:
        await service.create({"name": "   "}, actor_id=uuid4(), meta=META)

    assert err.value.details["errors"][0]["loc"] == ["name"]
    assert fake.created_kwargs == {}


async def test_create_passes_fields_and_audits() -> None:
    audit = FakeAudit()
    service, _session, _app, fake = _service(audit=audit)

    body = await service.create(
        {"name": "新应用", "greeting": "你好", "custom_dict": ["小优"]}, actor_id=uuid4(), meta=META
    )

    assert fake.created_kwargs["name"] == "新应用"
    assert fake.created_kwargs["greeting"] == "你好"
    assert fake.created_kwargs["custom_dict"] == ["小优"]
    assert body["name"] == "新建"
    assert audit.entries[0].action == "app.create"


async def test_copy_appends_the_suffix_and_omits_documents() -> None:
    created = FakeApp(id=uuid4(), name="售后客服（副本）")
    service, _session, app, fake = _service(created=created)
    app.custom_dict = ["小优"]

    body = await service.copy(app.id, actor_id=uuid4(), meta=META)

    assert fake.created_kwargs["name"] == "售后客服（副本）"
    assert fake.created_kwargs["custom_dict"] == ["小优"]
    assert "documents" not in fake.created_kwargs
    assert body["id"] == str(created.id)


async def test_delete_marks_revokes_closes_and_dispatches() -> None:
    calls: list[tuple[str, tuple[str, ...]]] = []
    audit = FakeAudit()
    service, session, app, _fake = _service(
        results=[_Result(values=["ak_1", "ak_2"]), _Result(rowcount=3)],
        audit=audit,
        enqueue=lambda task, args: calls.append((task, tuple(args))),
    )

    body = await service.delete(app.id, actor_id=uuid4(), meta=META)

    job = session.added[0]
    assert app.status == "deleting"
    assert job.type == "cleanup_app" and job.app_id == app.id
    assert body["revoked_keys"] == 2
    assert body["closed_conversations"] == 3
    assert calls == [("aics.cleanup_app", (str(app.id), str(job.id)))]
    assert audit.entries[0].action == "app.delete"


async def test_delete_drops_the_key_cache_when_redis_is_available() -> None:
    deleted: list[str] = []
    redis = SimpleNamespace(delete=lambda key: deleted.append(key))
    service, _session, app, _fake = _service(
        results=[_Result(values=["ak_1"]), _Result(rowcount=0)], redis=redis
    )

    await service.delete(app.id, actor_id=uuid4(), meta=META)

    assert deleted == ["aics:apikey:ak_1"]
