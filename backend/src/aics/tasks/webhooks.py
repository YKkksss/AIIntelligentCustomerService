"""出站 Webhook 任务（队列 `context`）：只调用 service。"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from aics.modules.webhooks.service import (
    DELIVER_TASK,
    RETRY_SWEEP_TASK,
    run_deliver_job,
    run_retry_sweep_job,
)
from aics.tasks.celery_app import celery_app


@celery_app.task(name=DELIVER_TASK, queue="context")
def deliver_webhook(delivery_id: str) -> dict[str, Any]:
    """投递一条 `webhook_deliveries`（失败按退避排下一次；见 `WebhookService.deliver`）。"""
    return asyncio.run(run_deliver_job(UUID(delivery_id)))


@celery_app.task(name=RETRY_SWEEP_TASK, queue="context")
def webhook_retry_sweep() -> dict[str, int]:
    """每 10 秒（beat）：把到期的 `pending` 投递重新入队（ADR-0016；不靠长 countdown）。"""
    due = asyncio.run(run_retry_sweep_job())
    for delivery_id in due:
        deliver_webhook.delay(delivery_id)
    return {"due": len(due)}
