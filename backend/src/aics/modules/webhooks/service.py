"""出站 Webhook 投递（M3.5 ③）：签名、SSRF 校验、退避重试（第三方接入指南 §7）。

一条投递的生命周期：`events.emit` 在业务事务里插一行 `pending` → `aics.deliver_webhook`
（队列 `context`）取一行投递 → 2xx 置 `delivered`；否则 `attempts += 1` 并按固定退避
10 s / 30 s / 2 min / 10 min / 30 min（共 6 次）排下一次 → 最后一次仍失败置 `failed`。
beat 的 `aics.webhook_retry_sweep` 每 10 秒把到期的 `pending` 重新入队（不依赖 Celery 的
长 `countdown`——RabbitMQ 默认 `consumer_timeout` 是 30 分钟）。

`apps` 与 `webhooks` 是互不依赖的独立模块（import-linter 合同），所以：

- 密钥的生成 / 加密在 `core.secretbox`（两边都能用），轮换由 `modules.apps` 写 `apps` 行；
- 投递时本模块直接读 `apps` 行拿 `webhook_config`（读 `db.models.App` 不算 import 业务模块）；
- 保存期的 SSRF 校验在 `modules.apps` 里调 `core.netguard`，投递前这里再校验一次。
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aics.core import metrics, netguard
from aics.core.config import Settings, get_settings
from aics.core.secretbox import InvalidToken, decrypt
from aics.db.base import new_id
from aics.db.models import App
from aics.modules.webhooks import signing
from aics.modules.webhooks.payloads import build_payload, iso
from aics.modules.webhooks.repository import WebhookDeliveryRepository
from aics.rag.config import WebhookConfig

log = structlog.get_logger()

#: 单次投递的 HTTP 超时（第三方接入指南 §7：5 秒内 2xx 才算成功）。
TIMEOUT_S = 5.0
#: 第 1→5 次重试的等待秒数；第 6 次（首次 + 5 次重试）仍失败就置 `failed`。
RETRY_DELAYS_S: tuple[int, ...] = (10, 30, 120, 600, 1800)
#: 总共尝试几次（首次 + 重试次数）。
MAX_ATTEMPTS = len(RETRY_DELAYS_S) + 1
#: 重试扫描一次最多取多少条（与 `WebhookDeliveryRepository.due_all_apps` 的默认值一致）。
RETRY_BATCH = 100
#: 重试扫描把取到的行推到多久以后（避免 10 秒后的下一轮又抢同一批）。
RETRY_CLAIM_S = 60

DELIVER_TASK = "aics.deliver_webhook"
RETRY_SWEEP_TASK = "aics.webhook_retry_sweep"

#: 发送器：`(url, body, headers) -> status_code`；测试注入假实现，生产走 httpx。
Sender = Callable[[str, bytes, dict[str, str]], Awaitable[int]]


@dataclass
class DeliveryResult:
    """一次投递尝试的结果（任务返回值 / 日志用）。"""

    status: str  # delivered / retry / failed / skipped / missing
    attempts: int = 0
    status_code: int | None = None
    error: str | None = None
    next_attempt_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "attempts": self.attempts,
            "status_code": self.status_code,
            "error": self.error,
            "next_attempt_at": iso(self.next_attempt_at) if self.next_attempt_at else None,
        }


@dataclass
class TestResult:
    """`POST /webhook/test` 的结果（同步发一次，不落表：ISSUE-59）。"""

    ok: bool
    status_code: int | None = None
    error: str | None = None
    elapsed_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status_code": self.status_code,
            "error": self.error,
            "elapsed_ms": self.elapsed_ms,
        }


async def _send_http(url: str, body: bytes, headers: dict[str, str]) -> int:
    """真实发送：`follow_redirects=False`（每一跳都要重新做 SSRF 校验，所以干脆不跟）。"""
    async with httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=False) as client:
        resp = await client.post(url, content=body, headers=headers)
        return resp.status_code


class WebhookService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings | None = None,
        repo: WebhookDeliveryRepository | None = None,
        sender: Sender | None = None,
    ) -> None:
        self.session = session
        self.repo = repo or WebhookDeliveryRepository(session)
        self.settings = settings or get_settings()
        self._sender: Sender = sender or _send_http

    # ------------------------------------------------------------ 投递
    async def deliver(self, delivery_id: UUID) -> DeliveryResult:
        """投递一条记录；幂等——只有 `pending` 的行会被处理。"""
        row = await self.repo.load_all_apps(delivery_id)
        if row is None:
            return DeliveryResult(status="missing")
        if row.status != "pending":
            return DeliveryResult(status="skipped", attempts=row.attempts)

        cfg = await self._webhook_of(row.app_id)
        now = datetime.now(UTC)
        if cfg.url is None or cfg.secret_enc is None:
            return await self._give_up(row, error="尚未配置 Webhook 地址或签名密钥")
        try:
            secret = decrypt(cfg.secret_enc, self.settings.data_encryption_key.get_secret_value())
        except InvalidToken:
            log.warning("webhook.secret_undecryptable", app_id=str(row.app_id))
            return await self._give_up(row, error="secret 无法解密")
        try:
            netguard.check_url(cfg.url, netguard.Allowlist.parse(self.settings.url_fetch_allowlist))
        except netguard.UrlNotAllowedError as e:
            # 地址不合规不重试：DNS 变了也不该往内网发
            return await self._give_up(row, error=e.message)

        body = json.dumps(row.payload, ensure_ascii=False, separators=(",", ":")).encode()
        timestamp = int(now.timestamp())
        headers = signing.headers(
            delivery_id=row.id,
            event=row.event or "",
            timestamp=timestamp,
            signature=signing.sign(secret, timestamp, body),
        )
        try:
            status_code = await self._sender(cfg.url, body, headers)
        except Exception as e:  # 网络错误 / 超时 / TLS：一律按失败重试
            log.warning(
                "webhook.delivery_failed",
                app_id=str(row.app_id),
                delivery_id=str(row.id),
                error_type=type(e).__name__,
            )
            return await self._record_attempt(row, now, status_code=None, error=type(e).__name__)
        if 200 <= status_code < 300:
            # 先取次数：`mark_delivered` 的 UPDATE 会同步到 session 里的这一行（attempts += 1）
            attempts = row.attempts + 1
            await self.repo.mark_delivered(row.app_id, row.id, at=now)
            await self.session.commit()
            metrics.inc("aics_webhook_delivered_total", app_id=str(row.app_id))
            return DeliveryResult(status="delivered", attempts=attempts, status_code=status_code)
        return await self._record_attempt(
            row, now, status_code=status_code, error=f"HTTP {status_code}"
        )

    async def redeliver(self, app_id: UUID, delivery_id: UUID) -> bool:
        """人工重发：`failed` / `delivered` → `pending`（`attempts` 归零）；返回是否改了行。"""
        changed = await self.repo.reset_for_redelivery(app_id, delivery_id, at=datetime.now(UTC))
        await self.session.commit()
        return bool(changed)

    # ------------------------------------------------------------ 重试扫描
    async def due(self, *, now: datetime | None = None, limit: int = RETRY_BATCH) -> list[UUID]:
        """到期该重试的投递 id，并把它们的 `next_attempt_at` 推到 60 秒后（防重复入队）。"""
        at = now or datetime.now(UTC)
        rows = await self.repo.due_all_apps(before=at, limit=limit)
        if not rows:
            return []
        upcoming = at + timedelta(seconds=RETRY_CLAIM_S)
        for delivery_id in rows:
            await self.repo.claim_all_apps(delivery_id, at=upcoming)
        await self.session.commit()
        return rows

    # ------------------------------------------------------------ 测试投递
    async def send_test(self, app_id: UUID) -> TestResult:
        """发一条 `event=ping`（同步返回对方状态码）；不写 `webhook_deliveries`（ISSUE-59）。"""
        cfg = await self._webhook_of(app_id)
        if cfg.url is None or cfg.secret_enc is None:
            raise netguard.UrlNotAllowedError(message="尚未配置 Webhook 地址或签名密钥")
        try:
            secret = decrypt(cfg.secret_enc, self.settings.data_encryption_key.get_secret_value())
        except InvalidToken:
            raise netguard.UrlNotAllowedError(message="签名密钥无法解密，请重新生成") from None
        netguard.check_url(cfg.url, netguard.Allowlist.parse(self.settings.url_fetch_allowlist))

        now = datetime.now(UTC)
        delivery_id = new_id()
        body = json.dumps(
            build_payload(
                delivery_id=delivery_id,
                event="ping",
                app_id=app_id,
                conversation_id=None,
                user_id=None,
                seq=0,
                occurred_at=now,
                data={"message": "AICS Webhook 测试投递"},
            ),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        timestamp = int(now.timestamp())
        headers = signing.headers(
            delivery_id=delivery_id,
            event="ping",
            timestamp=timestamp,
            signature=signing.sign(secret, timestamp, body),
        )
        started = time.monotonic()
        try:
            status_code = await self._sender(cfg.url, body, headers)
        except Exception as e:
            return TestResult(
                ok=False,
                error=type(e).__name__,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        return TestResult(
            ok=200 <= status_code < 300,
            status_code=status_code,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    # ------------------------------------------------------------ 内部
    async def _webhook_of(self, app_id: UUID) -> WebhookConfig:
        app = (await self.session.execute(select(App).where(App.id == app_id))).scalar_one_or_none()
        if app is None:
            return WebhookConfig()
        try:
            return WebhookConfig.model_validate(dict(app.webhook_config or {}))
        except ValueError:  # 坏配置当没配：不投递，也不让整轮炸
            log.warning("webhook.bad_config", app_id=str(app_id))
            return WebhookConfig()

    async def _give_up(self, row: Any, *, error: str | None) -> DeliveryResult:
        """不可重试的失败：记一次尝试并置 `failed`。"""
        attempts = row.attempts + 1
        await self.repo.mark_attempt(
            row.app_id,
            row.id,
            status_code=None,
            error=error,
            next_attempt_at=None,
            failed=True,
        )
        await self.session.commit()
        metrics.inc("aics_webhook_failed_total", app_id=str(row.app_id))
        log.warning(
            "webhook.delivery_gave_up",
            app_id=str(row.app_id),
            delivery_id=str(row.id),
            attempts=attempts,
            error=error,
        )
        return DeliveryResult(status="failed", attempts=attempts, error=error)

    async def _record_attempt(
        self, row: Any, now: datetime, *, status_code: int | None, error: str | None
    ) -> DeliveryResult:
        attempts = row.attempts + 1
        failed = attempts >= MAX_ATTEMPTS
        next_attempt = None if failed else now + timedelta(seconds=RETRY_DELAYS_S[attempts - 1])
        await self.repo.mark_attempt(
            row.app_id,
            row.id,
            status_code=status_code,
            error=error,
            next_attempt_at=next_attempt,
            failed=failed,
        )
        await self.session.commit()
        if failed:
            metrics.inc("aics_webhook_failed_total", app_id=str(row.app_id))
            log.warning(
                "webhook.delivery_gave_up",
                app_id=str(row.app_id),
                delivery_id=str(row.id),
                attempts=attempts,
                error=error,
            )
        else:
            metrics.inc("aics_webhook_retry_total", app_id=str(row.app_id))
        return DeliveryResult(
            status="failed" if failed else "retry",
            attempts=attempts,
            status_code=status_code,
            error=error,
            next_attempt_at=next_attempt,
        )


# ---------------------------------------------------------------- 任务体
def _engine_and_session(settings: Settings) -> tuple[Any, async_sessionmaker[AsyncSession]]:
    """每个任务跑在各自的 `asyncio.run` 里，所以引擎必须 NullPool 且用完即弃。"""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(settings.db_url, poolclass=NullPool)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def run_deliver_job(delivery_id: UUID, settings: Settings | None = None) -> dict[str, Any]:
    """`aics.deliver_webhook` 的任务体。"""
    s = settings or get_settings()
    engine, sessions = _engine_and_session(s)
    try:
        async with sessions() as session:
            result = await WebhookService(session, settings=s).deliver(delivery_id)
    finally:
        await engine.dispose()
    return result.as_dict()


async def run_retry_sweep_job(settings: Settings | None = None) -> list[str]:
    """`aics.webhook_retry_sweep` 的任务体：返回到期该重试的投递 id（由任务负责入队）。"""
    s = settings or get_settings()
    engine, sessions = _engine_and_session(s)
    try:
        async with sessions() as session:
            due = await WebhookService(session, settings=s).due()
    finally:
        await engine.dispose()
    return [str(i) for i in due]
