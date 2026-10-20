"""M4.3 ②：知识库写操作审计（`KnowledgeService._audit`）。

审计端口由 `modules.audit.PgAuditSink` 实现，这里用假 sink 验证三条约定：
有操作人与 sink 才写、条目字段完整、无操作人（开放接口）不写。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from aics.core.audit import AuditEntry
from aics.modules.knowledge.service import KnowledgeService

APP_ID = uuid4()
DOC_ID = uuid4()


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.added: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1


class FakeStorage:
    async def get(self, key: str) -> bytes:  # pragma: no cover - 审计用不到
        return b""

    async def put(self, key: str, data: bytes) -> None:  # pragma: no cover
        return None

    async def exists(self, key: str) -> bool:  # pragma: no cover
        return False

    async def delete(self, key: str) -> bool:  # pragma: no cover
        return True

    async def delete_prefix(self, prefix: str) -> int:  # pragma: no cover
        return 0


class FakeSink:
    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    async def write(self, session: Any, entry: AuditEntry) -> None:
        self.entries.append(entry)


def _service(*, audit: FakeSink | None) -> KnowledgeService:
    return KnowledgeService(FakeSession(), storage=FakeStorage(), audit=audit)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_audit_writes_entry_with_actor_and_target() -> None:
    sink = FakeSink()
    service = _service(audit=sink)

    await service._audit(
        "kb.document.delete",
        APP_ID,
        actor_id=uuid4(),
        target_id=DOC_ID,
        diff={"before": {"title": "退货政策"}},
    )

    assert len(sink.entries) == 1
    entry = sink.entries[0]
    assert entry.action == "kb.document.delete"
    assert entry.app_id == APP_ID and entry.target_id == DOC_ID
    assert entry.target_type == "document"
    assert entry.diff == {"before": {"title": "退货政策"}}


@pytest.mark.asyncio
async def test_audit_skips_without_actor() -> None:
    sink = FakeSink()
    service = _service(audit=sink)

    await service._audit("kb.document.create", APP_ID, actor_id=None, target_id=DOC_ID)

    assert sink.entries == []


@pytest.mark.asyncio
async def test_audit_skips_without_sink() -> None:
    service = _service(audit=None)

    await service._audit("kb.document.create", APP_ID, actor_id=uuid4(), target_id=DOC_ID)

    assert service.audit is None
