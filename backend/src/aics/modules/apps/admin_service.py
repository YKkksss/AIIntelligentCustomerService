"""应用管理的后台服务（M4.2 ②）：列表、详情、配置保存。

路由只负责解析参数与权限，业务都在这里；底下的合并 / 校验 / `config_version` 递增复用
`AppService`（M1.2 起就是配置的唯一入口），本模块额外补三件事：

1. **可见应用过滤**：super_admin 看全部，其余人只看 `user_app_roles` 里有角色的应用。
2. **乐观并发**：PATCH 可带 `expected_config_version`，不匹配抛 409（ISSUE-75）。
3. **副作用**：写审计 `app.update`；`custom_dict` 变化时建 `retokenize_app` 任务并投递
   （投递器由 composition root 注入，`modules.apps` 不能 import `aics.tasks`）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aics.core.audit import AuditEntry, AuditSink
from aics.core.errors import ConfigVersionConflictError
from aics.db.models import App, Chunk, Document, Job, UserAppRole
from aics.modules.apps.service import BASIC_FIELDS, CONFIG_FIELDS, AppConfig, AppService

log = structlog.get_logger()

RETOKENIZE_TASK = "aics.retokenize_app"
AUDIT_PATCH_FIELDS = (*BASIC_FIELDS, *CONFIG_FIELDS, "custom_dict", "webhook_config")

Enqueue = Any  # Callable[[str, Sequence[Any]], None]


@dataclass(frozen=True)
class ActorMeta:
    """审计要记的请求侧信息（IP / UA / request_id）；路由层从 `request` 里取。"""

    ip: str | None = None
    user_agent: str | None = None
    request_id: str | None = None


_NO_META = ActorMeta()


def app_body(app: App, cfg: AppConfig) -> dict[str, Any]:
    """应用对象（开放接口参考 §应用 的示例结构）；`stats` 由调用方补。"""
    return {
        "id": str(app.id),
        "name": app.name,
        "description": app.description,
        "status": app.status,
        "greeting": app.greeting,
        "system_prompt": app.system_prompt,
        "answer_style": app.answer_style,
        "fallback_text": app.fallback_text,
        "language": app.language,
        "rag_config": app.rag_config,
        "handoff_config": app.handoff_config,
        "session_config": app.session_config,
        "effective_session_config": cfg.effective_session.model_dump(mode="json"),
        "webhook_config": cfg.webhook.public(),
        "chunk_config": app.chunk_config,
        "custom_dict": list(app.custom_dict or []),
        "config_version": app.config_version,
        "chunk_config_version": app.chunk_config_version,
        "created_at": app.created_at,
        "updated_at": app.updated_at,
    }


def app_card(app: App, *, documents: int) -> dict[str, Any]:
    """列表卡片：够画一屏，不把整份配置搬出来。"""
    return {
        "id": str(app.id),
        "name": app.name,
        "description": app.description,
        "status": app.status,
        "answer_style": app.answer_style,
        "language": app.language,
        "config_version": app.config_version,
        "created_at": app.created_at,
        "updated_at": app.updated_at,
        "stats": {"documents": documents},
    }


class AppsAdminService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Any = None,
        audit: AuditSink | None = None,
        enqueue: Enqueue | None = None,
        platform_session: Any = None,
    ) -> None:
        kwargs: dict[str, Any] = {"settings": settings}
        if platform_session is not None:
            kwargs["platform_session"] = platform_session
        self.session = session
        self.apps = AppService(session, **kwargs)
        self.audit = audit
        self.enqueue = enqueue

    @classmethod
    async def load(
        cls,
        session: AsyncSession,
        *,
        settings: Any = None,
        audit: AuditSink | None = None,
        enqueue: Enqueue | None = None,
    ) -> AppsAdminService:
        """平台默认会话设置从 `system_settings` 读（与 `AppService.load` 同一来源）。"""
        loaded = await AppService.load(session, settings=settings)
        return cls(session, settings=settings, audit=audit, enqueue=enqueue).with_apps(loaded)

    def with_apps(self, apps: AppService) -> AppsAdminService:
        self.apps = apps
        return self

    # ------------------------------------------------------------ 读
    async def visible_ids(self, user_id: UUID, *, is_super_admin: bool) -> list[UUID] | None:
        """`None` 表示「全部应用」；否则是当前用户的成员应用 id 列表。"""
        if is_super_admin:
            return None
        stmt = select(UserAppRole.app_id).where(UserAppRole.user_id == user_id)
        return list((await self.session.execute(stmt)).scalars())

    async def list_apps(
        self, *, user_id: UUID, is_super_admin: bool, limit: int = 50, offset: int = 0
    ) -> list[dict[str, Any]]:
        ids = await self.visible_ids(user_id, is_super_admin=is_super_admin)
        if ids is not None and not ids:
            return []
        apps = await self.apps.list_apps(ids=ids, limit=limit, offset=offset)
        counts = await self._document_counts([a.id for a in apps])
        return [app_card(a, documents=counts.get(a.id, 0)) for a in apps]

    async def detail(self, app_id: UUID) -> dict[str, Any]:
        app = await self.apps.get_app(app_id)
        cfg = self.apps.config_of(app)
        body = app_body(app, cfg)
        body["stats"] = await self._stats(app_id)
        return body

    async def _document_counts(self, app_ids: Sequence[UUID]) -> dict[UUID, int]:
        if not app_ids:
            return {}
        stmt = (
            select(Document.app_id, func.count())
            .where(Document.app_id.in_(app_ids), Document.deleted_at.is_(None))
            .group_by(Document.app_id)
        )
        return {app_id: int(n) for app_id, n in (await self.session.execute(stmt)).all()}

    async def _stats(self, app_id: UUID) -> dict[str, int]:
        alive = select(Document.id).where(Document.app_id == app_id, Document.deleted_at.is_(None))
        documents = await self.session.scalar(
            select(func.count())
            .select_from(Document)
            .where(Document.app_id == app_id, Document.deleted_at.is_(None))
        )
        chunks = await self.session.scalar(
            select(func.count())
            .select_from(Chunk)
            .where(Chunk.app_id == app_id, Chunk.document_id.in_(alive))
        )
        flagged = await self.session.scalar(
            select(func.count())
            .select_from(Chunk)
            .where(
                Chunk.app_id == app_id,
                Chunk.document_id.in_(alive),
                Chunk.review_status == "flagged",
            )
        )
        return {
            "documents": int(documents or 0),
            "chunks": int(chunks or 0),
            "flagged_chunks": int(flagged or 0),
        }

    # ------------------------------------------------------------ 配置保存
    async def update(
        self,
        app_id: UUID,
        patch: Mapping[str, Any],
        *,
        expected_version: int | None,
        actor_id: UUID,
        meta: ActorMeta = _NO_META,
    ) -> dict[str, Any]:
        app = await self.apps.get_app(app_id)
        if expected_version is not None and expected_version != app.config_version:
            raise ConfigVersionConflictError(
                details={"expected": expected_version, "current": app.config_version}
            )
        before = {k: getattr(app, k) for k in AUDIT_PATCH_FIELDS if hasattr(app, k)}
        result = await self.apps.update_app(app_id, patch)
        after = {k: getattr(result.app, k) for k in before}
        changes = sorted(k for k in before if before[k] != after[k])
        job: Job | None = None
        if result.custom_dict_changed:
            job = Job(
                app_id=app_id,
                type="retokenize_app",
                target_id=app_id,
                payload={"reason": "admin.custom_dict"},
                created_by=actor_id,
            )
            self.session.add(job)
        if changes and self.audit is not None:
            await self.audit.write(
                self.session,
                AuditEntry(
                    action="app.update",
                    actor_id=actor_id,
                    app_id=app_id,
                    target_type="app",
                    target_id=app_id,
                    diff={
                        "before": {k: before[k] for k in changes},
                        "after": {k: after[k] for k in changes},
                    },
                    ip=meta.ip,
                    user_agent=meta.user_agent,
                    request_id=meta.request_id,
                ),
            )
        await self.session.commit()
        if job is not None:
            self._enqueue_retokenize(app_id, job.id)
        body = app_body(result.app, result.config)
        body["stats"] = await self._stats(app_id)
        body["reindex_required"] = result.reindex_required
        body["retokenize_job_id"] = None if job is None else str(job.id)
        return body

    def _enqueue_retokenize(self, app_id: UUID, job_id: UUID) -> None:
        if self.enqueue is None:
            return
        try:
            self.enqueue(RETOKENIZE_TASK, (str(app_id), str(job_id)))
        except Exception as e:  # noqa: BLE001 - 投递失败只影响即时性，任务行留待 sweep 兜底
            log.warning("apps.enqueue_failed", task=RETOKENIZE_TASK, error_type=type(e).__name__)
