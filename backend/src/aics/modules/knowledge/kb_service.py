"""公共知识库服务（M4.7 ③）。

知识库是平台级资源：这里只做「库」的增删改查、与客服的关联维护，以及默认库解析。
库里的文档仍由 `KnowledgeService` 管理（文档、切块、任务都以应用为作用域，默认库用于承接
旧路径），所以本模块不碰文档表。

删除策略：仍有文档或仍被客服关联时拒绝（409 `KNOWLEDGE_BASE_IN_USE`），先把文档迁走或解绑，
再删；删除是软删（`status='deleting'`），真正的清理留给后续步骤。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog
from pydantic import ValidationError
from sqlalchemy import func, select

from aics.core.audit import AuditEntry, AuditSink, diff
from aics.core.errors import (
    AppNotFoundError,
    KnowledgeBaseInUseError,
    KnowledgeBaseNameTakenError,
    KnowledgeBaseNotFoundError,
    ValidationAppError,
)
from aics.db.models.app import App
from aics.db.models.knowledge import Document, KnowledgeBase
from aics.modules.knowledge.kb_repository import (
    AppKBLinkRepository,
    KnowledgeBaseRepository,
    kb_row,
)
from aics.rag.config import ChunkConfig

log = structlog.get_logger(__name__)

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
NAME_MAX_CHARS = 100
DESCRIPTION_MAX_CHARS = 500
STATUSES = ("active", "disabled", "deleting")
DEFAULT_KB_SUFFIX = "默认知识库"

ACTION_CREATE = "kb.create"
ACTION_UPDATE = "kb.update"
ACTION_DELETE = "kb.delete"
ACTION_LINK = "kb.app.link"
TARGET = "knowledge_base"


@dataclass(frozen=True)
class ActorMeta:
    ip: str | None = None
    user_agent: str | None = None
    request_id: str | None = None


_NO_META = ActorMeta()


def _chunk_config(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """校验库级切块预设；错误定位在 `chunk_config.*`，与文档接口的形状一致。"""
    try:
        return ChunkConfig.model_validate(dict(raw or {})).model_dump(mode="json")
    except ValidationError as exc:
        raise ValidationAppError(
            message="切块参数校验失败",
            details={
                "errors": [
                    {"loc": ["chunk_config", *err["loc"]], "msg": err["msg"]}
                    for err in exc.errors()
                ]
            },
        ) from None


def _name(raw: str | None) -> str:
    name = (raw or "").strip()
    if not name or len(name) > NAME_MAX_CHARS:
        raise ValidationAppError(
            message="参数校验失败",
            details={"errors": [{"loc": ["name"], "msg": f"不能为空且不超过 {NAME_MAX_CHARS} 字"}]},
        )
    return name


class KnowledgeBaseService:
    """库的读写与关联；审计走 core 端口，不依赖 audit 模块。"""

    def __init__(self, session: Any, *, audit: AuditSink | None = None) -> None:
        self.session = session
        self.repo = KnowledgeBaseRepository(session)
        self.links = AppKBLinkRepository(session)
        self.audit = audit

    # ---------------------------------------------------------------- 读
    async def list(
        self,
        *,
        status: str | None = None,
        q: str | None = None,
        page: int = 1,
        page_size: int = DEFAULT_LIMIT,
    ) -> tuple[list[dict[str, Any]], int]:
        size = max(1, min(int(page_size), MAX_LIMIT))
        rows, total = await self.repo.list_page(
            status=status, q=(q or "").strip() or None, page=max(1, int(page)), page_size=size
        )
        counts = await self.repo.counts([row.id for row in rows])
        apps = await self._apps_of([row.id for row in rows])
        return [
            kb_row(
                row,
                documents=counts.get(row.id, (0, 0))[0],
                chunks=counts.get(row.id, (0, 0))[1],
                apps=apps.get(row.id, []),
            )
            for row in rows
        ], total

    async def detail(self, kb_id: UUID) -> dict[str, Any]:
        kb = await self._get(kb_id)
        counts = await self.repo.counts([kb_id])
        apps = await self._apps_of([kb_id])
        documents, chunks = counts.get(kb_id, (0, 0))
        return kb_row(kb, documents=documents, chunks=chunks, apps=apps.get(kb_id, []))

    async def links_of(self, app_id: UUID) -> list[dict[str, Any]]:
        await self._app(app_id)
        kb_ids = await self.links.kb_ids(app_id)
        rows = await self.repo.get_many(kb_ids)
        counts = await self.repo.counts(kb_ids)
        marked = await self.links.default_kb_id(app_id)
        order = {kb_id: index for index, kb_id in enumerate(kb_ids)}
        rows.sort(key=lambda row: order.get(row.id, 0))
        return [
            kb_row(
                row,
                documents=counts.get(row.id, (0, 0))[0],
                chunks=counts.get(row.id, (0, 0))[1],
                linked=True,
                is_default=row.id == marked,
            )
            for row in rows
        ]

    async def default_kb_of(self, app_id: UUID) -> UUID | None:
        """应用的默认库：显式标记优先，其次按名字找迁移期回填的库，最后取最早的关联。"""
        marked = await self.links.default_kb_id(app_id)
        if marked is not None:
            return marked
        app = await self.session.get(App, app_id)
        if app is None:
            return None
        stmt = select(KnowledgeBase.id).where(
            KnowledgeBase.name == f"{app.name} {DEFAULT_KB_SUFFIX}",
            KnowledgeBase.status != "deleting",
        )
        found = (await self.session.execute(stmt)).scalar_one_or_none()
        if found is not None:
            return found
        kb_ids = await self.links.kb_ids(app_id)
        return kb_ids[0] if kb_ids else None

    # ---------------------------------------------------------------- 写
    async def create(
        self,
        *,
        name: str,
        description: str = "",
        chunk_config: Mapping[str, Any] | None = None,
        actor_id: UUID | None = None,
        meta: ActorMeta = _NO_META,
    ) -> dict[str, Any]:
        clean = _name(name)
        if await self.repo.by_name(clean) is not None:
            raise KnowledgeBaseNameTakenError(details={"name": clean})
        body = (description or "").strip()
        if len(body) > DESCRIPTION_MAX_CHARS:
            raise ValidationAppError(
                message="参数校验失败",
                details={
                    "errors": [
                        {
                            "loc": ["description"],
                            "msg": f"不超过 {DESCRIPTION_MAX_CHARS} 字",
                        }
                    ]
                },
            )
        kb = KnowledgeBase(
            name=clean,
            description=body,
            chunk_config=_chunk_config(chunk_config),
            created_by=actor_id,
        )
        await self.repo.create(kb)
        await self._audit(ACTION_CREATE, kb.id, {"name": clean}, actor_id=actor_id, meta=meta)
        await self.session.commit()
        await self.session.refresh(kb)
        return await self.detail(kb.id)

    async def update(
        self,
        kb_id: UUID,
        patch: Mapping[str, Any],
        *,
        actor_id: UUID | None = None,
        meta: ActorMeta = _NO_META,
    ) -> dict[str, Any]:
        kb = await self._get(kb_id)
        unknown = set(patch) - {"name", "description", "chunk_config", "status"}
        if unknown:
            raise ValidationAppError(
                message="参数校验失败",
                details={"errors": [{"loc": [key], "msg": "未知字段"} for key in sorted(unknown)]},
            )
        before = {
            "name": kb.name,
            "description": kb.description,
            "chunk_config": dict(kb.chunk_config or {}),
            "status": kb.status,
        }
        values: dict[str, Any] = {}
        if "name" in patch:
            clean = _name(patch["name"])
            other = await self.repo.by_name(clean)
            if other is not None and other.id != kb.id:
                raise KnowledgeBaseNameTakenError(details={"name": clean})
            values["name"] = clean
        if "description" in patch:
            body = (patch["description"] or "").strip()
            if len(body) > DESCRIPTION_MAX_CHARS:
                raise ValidationAppError(
                    message="参数校验失败",
                    details={
                        "errors": [
                            {"loc": ["description"], "msg": f"不超过 {DESCRIPTION_MAX_CHARS} 字"}
                        ]
                    },
                )
            values["description"] = body
        if "chunk_config" in patch:
            cfg = _chunk_config(patch["chunk_config"])
            if cfg != before["chunk_config"]:
                values["chunk_config"] = cfg
                values["config_version"] = kb.config_version + 1
        if "status" in patch:
            status = str(patch["status"])
            if status not in STATUSES:
                raise ValidationAppError(
                    message="参数校验失败",
                    details={"errors": [{"loc": ["status"], "msg": "取值不合法"}]},
                )
            values["status"] = status
        if not values:
            return await self.detail(kb.id)
        values["updated_at"] = func.now()
        await self.repo.save(kb.id, **values)
        after = {**before, **{k: v for k, v in values.items() if k != "updated_at"}}
        await self._audit(
            ACTION_UPDATE,
            kb.id,
            diff(before, after),
            actor_id=actor_id,
            meta=meta,
        )
        await self.session.commit()
        return await self.detail(kb.id)

    async def delete(
        self,
        kb_id: UUID,
        *,
        actor_id: UUID | None = None,
        meta: ActorMeta = _NO_META,
    ) -> None:
        kb = await self._get(kb_id)
        apps = await self.links.apps_of_kb_all_apps(kb_id)
        docs = int(
            (
                await self.session.execute(
                    select(func.count())
                    .select_from(Document)
                    .where(Document.kb_id == kb_id, Document.deleted_at.is_(None))
                )
            ).scalar_one()
        )
        if apps or docs:
            raise KnowledgeBaseInUseError(details={"apps": len(apps), "documents": docs})
        await self.repo.save(kb_id, status="deleting", updated_at=func.now())
        await self._audit(ACTION_DELETE, kb.id, {"name": kb.name}, actor_id=actor_id, meta=meta)
        await self.session.commit()

    async def set_links(
        self,
        app_id: UUID,
        kb_ids: Sequence[UUID],
        *,
        default_kb_id: UUID | None = None,
        actor_id: UUID | None = None,
        meta: ActorMeta = _NO_META,
    ) -> list[dict[str, Any]]:
        await self._app(app_id)
        wanted = list(dict.fromkeys(kb_ids))
        rows = await self.repo.get_many(wanted)
        found = {row.id: row for row in rows}
        missing = [kb_id for kb_id in wanted if kb_id not in found]
        if missing:
            raise KnowledgeBaseNotFoundError(details={"kb_ids": [str(kb_id) for kb_id in missing]})
        broken = [row.id for row in rows if row.status == "deleting"]
        if broken:
            raise KnowledgeBaseInUseError(details={"kb_ids": [str(kb_id) for kb_id in broken]})
        added, removed = await self.links.replace(
            app_id, wanted, created_by=actor_id, default_kb_id=default_kb_id
        )
        if added or removed:
            await self._audit_link(app_id, added, removed, actor_id=actor_id, meta=meta)
        await self.session.commit()
        return await self.links_of(app_id)

    # ---------------------------------------------------------------- 内部
    async def _get(self, kb_id: UUID) -> KnowledgeBase:
        kb = await self.repo.get(kb_id)
        if kb is None or kb.status == "deleting":
            raise KnowledgeBaseNotFoundError(details={"kb_id": str(kb_id)})
        return kb

    async def _app(self, app_id: UUID) -> App:
        app = await self.session.get(App, app_id)
        if app is None or app.deleted_at is not None or app.status == "deleting":
            raise AppNotFoundError(details={"app_id": str(app_id)})
        return app

    async def _apps_of(self, kb_ids: Sequence[UUID]) -> dict[UUID, list[dict[str, str]]]:
        if not kb_ids:
            return {}
        result: dict[UUID, list[dict[str, str]]] = {}
        for kb_id in kb_ids:
            app_ids = await self.links.apps_of_kb_all_apps(kb_id)
            if not app_ids:
                continue
            stmt = select(App.id, App.name).where(App.id.in_(app_ids))
            result[kb_id] = [
                {"app_id": str(row[0]), "name": row[1]}
                for row in (await self.session.execute(stmt)).all()
            ]
        return result

    async def _audit(
        self,
        action: str,
        kb_id: UUID,
        payload: Mapping[str, Any] | None,
        *,
        actor_id: UUID | None,
        meta: ActorMeta,
    ) -> None:
        if self.audit is None or actor_id is None:
            return
        await self.audit.write(
            self.session,
            AuditEntry(
                action=action,
                actor_type="user",
                actor_id=actor_id,
                app_id=None,
                target_type=TARGET,
                target_id=kb_id,
                diff=dict(payload or {}),
                ip=meta.ip,
                user_agent=meta.user_agent,
                request_id=meta.request_id,
            ),
        )

    async def _audit_link(
        self,
        app_id: UUID,
        added: Sequence[UUID],
        removed: Sequence[UUID],
        *,
        actor_id: UUID | None,
        meta: ActorMeta,
    ) -> None:
        if self.audit is None or actor_id is None:
            return
        await self.audit.write(
            self.session,
            AuditEntry(
                action=ACTION_LINK,
                actor_type="user",
                actor_id=actor_id,
                app_id=app_id,
                target_type="app",
                target_id=app_id,
                diff={
                    "before": {"kb_ids": [str(k) for k in removed]},
                    "after": {"kb_ids": [str(k) for k in added]},
                },
                ip=meta.ip,
                user_agent=meta.user_agent,
                request_id=meta.request_id,
            ),
        )
