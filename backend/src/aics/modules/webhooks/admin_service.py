"""`modules.webhooks` 的管理接口服务（M4.5 ④）：投递记录、重发、发送测试与密钥轮换。

模块独立性（ADR-0017 / import-linter 第 8 条）：`webhooks` **不能** import `modules.apps`，
所以这里直接读写 `apps` 表的两列 `webhook_config`，加密工具用 `core.secretbox`；审计只走
`core.audit.AuditSink`。与 `apps` 的 Webhook 配置页是同一份数据，字段结构由 `WebhookConfig`
（`rag/config.py`）定义，两边共用。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aics.core import secretbox
from aics.core.audit import AuditEntry, AuditSink, diff
from aics.core.config import Settings
from aics.core.errors import AppNotFoundError, NotFoundError
from aics.db.models.app import App
from aics.modules.webhooks.repository import WebhookDeliveryRepository
from aics.modules.webhooks.service import WebhookService
from aics.rag.config import WebhookConfig

TARGET = "webhook_delivery"
TEST_ACTION = "webhook.test"
REDELIVER_ACTION = "webhook.redeliver"
SECRET_ACTION = "webhook.secret"  # noqa: S105 - 审计动作名，不是口令
DEFAULT_LIMIT = 20
MAX_LIMIT = 100


@dataclass(frozen=True)
class ActorMeta:
    """审计补充信息（与其它管理接口一致）。"""

    ip: str | None = None
    user_agent: str | None = None
    request_id: str | None = None


_NO_META = ActorMeta()


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def delivery_row(row: Any) -> dict[str, Any]:
    """投递记录视图：只回状态与错误，不回 payload（可能含客户数据）。"""
    return {
        "id": str(row.id),
        "event": row.event,
        "seq": row.seq,
        "conversation_id": None if row.conversation_id is None else str(row.conversation_id),
        "status": row.status,
        "attempts": row.attempts,
        "status_code": row.last_status_code,
        "error": row.last_error,
        "next_attempt_at": _iso(row.next_attempt_at),
        "delivered_at": _iso(row.delivered_at),
        "created_at": _iso(row.created_at),
    }


def webhook_body(cfg: WebhookConfig) -> dict[str, Any]:
    """应用当前的 Webhook 配置（不含密文，只回前缀）。"""
    return {
        "url": cfg.url,
        "secret_prefix": cfg.secret_prefix,
        "ready": cfg.url is not None and cfg.secret_enc is not None,
    }


@dataclass
class WebhookAdminService:
    session: AsyncSession
    settings: Settings
    audit: AuditSink | None = None

    def __post_init__(self) -> None:
        self.repo = WebhookDeliveryRepository(self.session)
        self.deliverer = WebhookService(self.session, settings=self.settings, repo=self.repo)

    async def config(self, app_id: UUID) -> dict[str, Any]:
        return webhook_body(self._config_of(await self._app(app_id)))

    async def deliveries(
        self, app_id: UUID, *, status: str | None, limit: int, before: datetime | None
    ) -> list[dict[str, Any]]:
        await self._app(app_id)  # 应用不存在 / 删除中 → 404
        rows = await self.repo.list_recent(
            app_id, status=status, limit=min(max(limit, 1), MAX_LIMIT), before=before
        )
        return [delivery_row(row) for row in rows]

    async def redeliver(
        self,
        app_id: UUID,
        delivery_id: UUID,
        *,
        actor_id: UUID | None,
        meta: ActorMeta = _NO_META,
    ) -> dict[str, Any]:
        before = await self.repo.get(app_id, delivery_id)
        if before is None or before.status == "pending":
            raise NotFoundError(
                message="投递记录不存在或已在队列中",
                details={"delivery_id": str(delivery_id)},
            )
        before_status = before.status  # 先取值：复位后 ORM 对象可能被刷新
        await self.deliverer.redeliver(app_id, delivery_id)
        await self._audit(
            REDELIVER_ACTION,
            app_id,
            actor_id=actor_id,
            target_id=delivery_id,
            meta=meta,
            payload=diff({"status": before_status}, {"status": "pending"}),
        )
        await self.session.commit()
        refreshed = await self.repo.get(app_id, delivery_id)
        if refreshed is None:  # 刚重发的行不可能消失；防御性兜底
            raise NotFoundError(
                message="投递记录不存在或不属于本应用",
                details={"delivery_id": str(delivery_id)},
            )
        return delivery_row(refreshed)

    async def test(
        self, app_id: UUID, *, actor_id: UUID | None, meta: ActorMeta = _NO_META
    ) -> dict[str, Any]:
        await self._app(app_id)
        result = await self.deliverer.send_test(app_id)
        await self._audit(
            TEST_ACTION,
            app_id,
            actor_id=actor_id,
            target_id=None,
            meta=meta,
            payload={"ok": result.ok, "status_code": result.status_code},
        )
        await self.session.commit()
        return result.as_dict()

    async def rotate_secret(
        self, app_id: UUID, *, actor_id: UUID | None, meta: ActorMeta = _NO_META
    ) -> dict[str, Any]:
        """生成（或轮换）签名密钥，明文只在这里返回一次；库里只留密文与前缀。"""
        app = await self._app(app_id)
        cfg = self._config_of(app)
        plain = secretbox.new_secret()
        encrypted = secretbox.encrypt(plain, self.settings.data_encryption_key.get_secret_value())
        values = cfg.model_dump()
        values.update(secret_enc=encrypted, secret_prefix=secretbox.prefix_of(plain))
        # JSONB 列没有 MutableDict：必须整体赋值才会被 SQLAlchemy 记为变更
        app.webhook_config = values
        await self._audit(
            SECRET_ACTION,
            app_id,
            actor_id=actor_id,
            target_id=None,
            meta=meta,
            payload=diff(
                {"secret_prefix": cfg.secret_prefix},
                {"secret_prefix": values["secret_prefix"]},
            ),
        )
        await self.session.commit()
        return {"secret": plain, "secret_prefix": values["secret_prefix"]}

    async def _app(self, app_id: UUID) -> App:
        app = (await self.session.execute(select(App).where(App.id == app_id))).scalar_one_or_none()
        if app is None or app.deleted_at is not None or app.status == "deleting":
            raise AppNotFoundError()
        return app

    @staticmethod
    def _config_of(app: App) -> WebhookConfig:
        return WebhookConfig.model_validate(dict(app.webhook_config or {}))

    async def _audit(
        self,
        action: str,
        app_id: UUID,
        *,
        actor_id: UUID | None,
        target_id: UUID | None,
        meta: ActorMeta,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        if self.audit is None or actor_id is None:
            return
        await self.audit.write(
            self.session,
            AuditEntry(
                action=action,
                actor_id=actor_id,
                app_id=app_id,
                target_type=TARGET,
                target_id=target_id,
                diff=dict(payload) if payload else None,
                ip=meta.ip,
                user_agent=meta.user_agent,
                request_id=meta.request_id,
            ),
        )
