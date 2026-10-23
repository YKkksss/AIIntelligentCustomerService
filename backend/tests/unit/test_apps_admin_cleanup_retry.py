"""M4.8 ③：`POST /apps/{id}/cleanup/retry` 的重跑语义（ISSUE-78）。"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from aics.core.errors import ConflictError, NotFoundError
from aics.modules.apps.admin_service import ActorMeta, AppsAdminService


@dataclass
class FakeApp:
    id: UUID
    status: str = "deleting"


class FakeApps:
    def __init__(self, app: FakeApp | None) -> None:
        self.app = app

    async def get_app(self, _app_id: UUID) -> FakeApp:
        if self.app is None:
            raise NotFoundError(message="应用不存在")
        return self.app


class _Result:
    def __init__(self, row: Any) -> None:
        self._row = row

    def scalar_one_or_none(self) -> Any:
        return self._row


class FakeSession:
    def __init__(self, running: Any = None) -> None:
        self.running = running
        self.added: list[Any] = []
        self.commits = 0
        self.flushes = 0

    async def execute(self, _stmt: Any) -> _Result:
        return _Result(self.running)

    def add(self, row: Any) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        self.flushes += 1
        for row in self.added:  # 模拟 `Job.id` 的 default=new_id 在 flush 时才生效
            if getattr(row, "id", None) is None:
                row.id = uuid4()

    async def commit(self) -> None:
        self.commits += 1


class FakeAudit:
    def __init__(self) -> None:
        self.entries: list[Any] = []

    async def write(self, _session: Any, entry: Any) -> None:
        self.entries.append(entry)


def _service(session: FakeSession, app: FakeApp | None, *, audit=None, enqueue=None):
    svc = AppsAdminService(session, settings=SimpleNamespace(), audit=audit, enqueue=enqueue)
    svc.apps = FakeApps(app)  # type: ignore[assignment] - 只替换仓储入口，方法体照跑
    return svc


async def test_retry_rejects_an_app_that_is_not_deleting() -> None:
    app = FakeApp(id=uuid4(), status="active")
    session = FakeSession()
    with pytest.raises(ConflictError) as err:
        await _service(session, app).retry_cleanup(app.id, actor_id=uuid4())
    assert err.value.details == {"status": "active"}
    assert session.added == [] and session.commits == 0


async def test_retry_rejects_a_cleanup_that_is_already_queued() -> None:
    app = FakeApp(id=uuid4())
    running = SimpleNamespace(id=uuid4(), status="running")
    session = FakeSession(running=running)
    with pytest.raises(ConflictError) as err:
        await _service(session, app).retry_cleanup(app.id, actor_id=uuid4())
    assert err.value.details == {"job_id": str(running.id), "status": "running"}
    assert session.added == []


async def test_retry_unknown_app_is_404() -> None:
    with pytest.raises(NotFoundError):
        await _service(FakeSession(), None).retry_cleanup(uuid4(), actor_id=uuid4())


async def test_retry_creates_a_job_enqueues_and_audits() -> None:
    app = FakeApp(id=uuid4())
    actor_id = uuid4()
    session = FakeSession()
    audit = FakeAudit()
    sent: list[tuple[str, Any]] = []
    svc = _service(session, app, audit=audit, enqueue=lambda task, args: sent.append((task, args)))

    body = await svc.retry_cleanup(app.id, actor_id=actor_id, meta=ActorMeta(ip="127.0.0.1"))

    job = session.added[0]
    assert body == {"job_id": str(job.id), "status": "queued"}
    assert job.type == "cleanup_app" and job.payload == {"reason": "admin.retry"}
    assert job.app_id == app.id and job.target_id == app.id and job.created_by == actor_id
    assert session.flushes == 1 and session.commits == 1
    assert sent == [("aics.cleanup_app", (str(app.id), str(job.id)))]
    assert audit.entries[0].action == "app.cleanup.retry"
    assert str(audit.entries[0].target_id) == str(app.id)


async def test_retry_survives_an_enqueue_failure() -> None:
    app = FakeApp(id=uuid4())
    session = FakeSession()

    def boom(_task: str, _args: Any) -> None:
        raise RuntimeError("no worker")

    body = await _service(session, app, enqueue=boom).retry_cleanup(app.id, actor_id=uuid4())
    assert body["status"] == "queued" and session.commits == 1
