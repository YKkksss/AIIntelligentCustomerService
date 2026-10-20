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
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aics.core.audit import AuditEntry, AuditSink
from aics.core.errors import ConfigVersionConflictError, ValidationAppError
from aics.db.models import ApiKey, App, Chunk, Conversation, Document, Job, UserAppRole
from aics.modules.apps.service import BASIC_FIELDS, CONFIG_FIELDS, AppConfig, AppService
from aics.modules.apps.system_settings import DEFAULT_SESSION_CONFIG_KEY, SystemSettingsService

log = structlog.get_logger()

RETOKENIZE_TASK = "aics.retokenize_app"
CLEANUP_TASK = "aics.cleanup_app"
COPY_SUFFIX = "（副本）"
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
        redis: Any = None,
        redis_prefix: str = "aics:",
    ) -> None:
        kwargs: dict[str, Any] = {"settings": settings}
        if platform_session is not None:
            kwargs["platform_session"] = platform_session
        self.session = session
        self.apps = AppService(session, **kwargs)
        self.audit = audit
        self.enqueue = enqueue
        self.redis = redis
        self.redis_prefix = redis_prefix

    @classmethod
    async def load(
        cls,
        session: AsyncSession,
        *,
        settings: Any = None,
        audit: AuditSink | None = None,
        enqueue: Enqueue | None = None,
        redis: Any = None,
        redis_prefix: str = "aics:",
    ) -> AppsAdminService:
        """平台默认会话设置从 `system_settings` 读（与 `AppService.load` 同一来源）。"""
        loaded = await AppService.load(session, settings=settings)
        return cls(
            session,
            settings=settings,
            audit=audit,
            enqueue=enqueue,
            redis=redis,
            redis_prefix=redis_prefix,
        ).with_apps(loaded)

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
        self._enqueue(RETOKENIZE_TASK, (str(app_id), str(job_id)), "apps.enqueue_failed")

    def _enqueue(self, task: str, args: Sequence[Any], event: str) -> None:
        if self.enqueue is None:
            return
        try:
            self.enqueue(task, args)
        except Exception as e:  # noqa: BLE001 - 投递失败只影响即时性，任务行留待 sweep 兜底
            log.warning(event, task=task, error_type=type(e).__name__)

    # ------------------------------------------------------------ 创建 / 复制 / 删除
    async def create(
        self,
        payload: Mapping[str, Any],
        *,
        actor_id: UUID,
        meta: ActorMeta = _NO_META,
    ) -> dict[str, Any]:
        """`POST /apps`：默认配置起步，字段校验全部交给 `AppService.create_app`。"""
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValidationAppError(details={"errors": [{"loc": ["name"], "msg": "名称不能为空"}]})
        app = await self._create_from(payload, name=name.strip(), actor_id=actor_id)
        await self._record(
            actor_id, meta, "app.create", app.id, {"before": {}, "after": {"name": name}}
        )
        await self.session.commit()  # `_create_from` 已提交过，审计行要在同一请求里落库
        return await self.detail(app.id)

    async def copy(
        self, app_id: UUID, *, actor_id: UUID, meta: ActorMeta = _NO_META
    ) -> dict[str, Any]:
        """`POST /apps/{id}/copy`：只复制基础信息与配置，不复制文档 / Key / 会话 / 密钥。"""
        source = await self.apps.get_app(app_id)
        payload: dict[str, Any] = {
            "name": f"{source.name}{COPY_SUFFIX}",
            "description": source.description,
            "greeting": source.greeting,
            "system_prompt": source.system_prompt,
            "answer_style": source.answer_style,
            "fallback_text": source.fallback_text,
            "language": source.language,
            "rag_config": source.rag_config,
            "handoff_config": source.handoff_config,
            "session_config": source.session_config,
            "chunk_config": source.chunk_config,
            "custom_dict": list(source.custom_dict or []),
        }
        app = await self._create_from(payload, name=payload["name"], actor_id=actor_id)
        await self._record(
            actor_id,
            meta,
            "app.copy",
            app.id,
            {"before": {}, "after": {"name": payload["name"], "source_app_id": str(app_id)}},
        )
        await self.session.commit()  # 同 `create`：审计行不能被请求结束时回滚掉
        return await self.detail(app.id)

    async def _create_from(self, payload: Mapping[str, Any], *, name: str, actor_id: UUID) -> App:
        basic = {
            k: payload[k]
            for k in BASIC_FIELDS
            if k != "name" and k in payload and payload[k] is not None
        }
        app = await self.apps.create_app(
            name=name,
            created_by=actor_id,
            rag_config=payload.get("rag_config"),
            handoff_config=payload.get("handoff_config"),
            session_config=payload.get("session_config"),
            chunk_config=payload.get("chunk_config"),
            custom_dict=list(payload.get("custom_dict") or []),
            **basic,
        )
        await self.session.commit()
        return app

    async def delete(
        self, app_id: UUID, *, actor_id: UUID, meta: ActorMeta = _NO_META
    ) -> dict[str, Any]:
        """`DELETE /apps/{id}`：`deleting` + 吊销 Key + 关闭会话，然后投递清理任务（202）。"""
        app = await self.apps.get_app(app_id)
        previous = app.status
        app.status = "deleting"
        prefixes = list(
            (
                await self.session.execute(
                    update(ApiKey)
                    .where(ApiKey.app_id == app_id, ApiKey.revoked_at.is_(None))
                    .values(revoked_at=func.now())
                    .returning(ApiKey.prefix)
                )
            ).scalars()
        )
        closed = int(
            (
                await self.session.execute(
                    update(Conversation)
                    .where(Conversation.app_id == app_id, Conversation.status == "active")
                    .values(status="closed", close_reason="app_deleted", closed_at=func.now())
                )
            ).rowcount  # type: ignore[attr-defined]
        )
        job = Job(
            app_id=app_id,
            type="cleanup_app",
            target_id=app_id,
            payload={"reason": "admin.delete"},
            created_by=actor_id,
        )
        self.session.add(job)
        await self._record(
            actor_id,
            meta,
            "app.delete",
            app_id,
            {
                "before": {"status": previous},
                "after": {"status": "deleting", "revoked_keys": len(prefixes), "closed": closed},
            },
        )
        await self.session.commit()
        self._drop_key_cache(prefixes)
        self._enqueue(CLEANUP_TASK, (str(app_id), str(job.id)), "apps.cleanup_enqueue_failed")
        return {
            "job_id": str(job.id),
            "revoked_keys": len(prefixes),
            "closed_conversations": closed,
        }

    async def system_settings(self) -> dict[str, Any]:
        """`GET /system/settings`：v1 只有平台默认会话配置（代码默认值兜底）。"""
        cfg = await SystemSettingsService(self.session).get_default_session_config()
        return {DEFAULT_SESSION_CONFIG_KEY: cfg.model_dump(mode="json")}

    async def update_system_settings(
        self, payload: Mapping[str, Any], *, actor_id: UUID, meta: ActorMeta = _NO_META
    ) -> dict[str, Any]:
        """`PATCH /system/settings`：校验、落库并写 `system.update` 审计。"""
        value = payload.get(DEFAULT_SESSION_CONFIG_KEY)
        if not isinstance(value, Mapping):
            raise ValidationAppError(
                details={"errors": [{"loc": [DEFAULT_SESSION_CONFIG_KEY], "msg": "必须是对象"}]}
            )
        service = SystemSettingsService(self.session)
        before = (await service.get_default_session_config()).model_dump(mode="json")
        # PATCH 语义：只给部分字段也要成立，合并现值后再整对象校验（否则必填字段报 422）
        merged = {**before, **dict(value)}
        after = (await service.set_default_session_config(merged, updated_by=actor_id)).model_dump(
            mode="json"
        )
        changed = [k for k in after if before.get(k) != after.get(k)]
        if changed and self.audit is not None:
            await self.audit.write(
                self.session,
                AuditEntry(
                    action="system.update",
                    actor_id=actor_id,
                    app_id=None,
                    target_type="system",
                    target_id=None,
                    diff={
                        "before": {k: before.get(k) for k in changed},
                        "after": {k: after[k] for k in changed},
                    },
                    ip=meta.ip,
                    user_agent=meta.user_agent,
                    request_id=meta.request_id,
                ),
            )
        await self.session.commit()
        return {DEFAULT_SESSION_CONFIG_KEY: after}

    async def _record(
        self,
        actor_id: UUID,
        meta: ActorMeta,
        action: str,
        app_id: UUID,
        diff: dict[str, Any],
    ) -> None:
        if self.audit is None:
            return
        await self.audit.write(
            self.session,
            AuditEntry(
                action=action,
                actor_id=actor_id,
                app_id=app_id,
                target_type="app",
                target_id=app_id,
                diff=diff,
                ip=meta.ip,
                user_agent=meta.user_agent,
                request_id=meta.request_id,
            ),
        )

    def _drop_key_cache(self, prefixes: Sequence[str]) -> None:
        """吊销后删掉鉴权缓存，避免最长 60 秒里旧 Key 还能用。"""
        if self.redis is None or not prefixes:
            return
        try:
            for prefix in prefixes:
                self.redis.delete(f"{self.redis_prefix}apikey:{prefix}")
        except Exception as e:  # noqa: BLE001 - 缓存删不掉只是延迟生效，下一次校验会回表
            log.warning("apps.key_cache_drop_failed", error_type=type(e).__name__)
