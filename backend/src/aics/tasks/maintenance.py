"""维护任务（队列 maintenance）：只调用 service。"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from aics.ingest.service import run_cleanup_job, run_reconcile_job, run_sweep_job
from aics.modules.apikeys.keyusage import KEY_USAGE_FLUSH_TASK, run_key_usage_flush_job
from aics.modules.chat.lifecycle import IDLE_SWEEP_TASK, run_idle_sweep_job
from aics.tasks.celery_app import celery_app


@celery_app.task(name="aics.cleanup_document", queue="maintenance")
def cleanup_document(
    document_id: str,
    below_index_version: int | None = None,
    old_storage_key: str | None = None,
    job_id: str | None = None,
) -> str:
    """删除旧版本的点 / 切块 / 父块 / 旧文件；文档 deleting 时删除全部并删文档行。"""
    return asyncio.run(
        run_cleanup_job(
            UUID(document_id),
            below_index_version,
            old_storage_key,
            UUID(job_id) if job_id else None,
        )
    )


@celery_app.task(name="aics.reconcile", queue="maintenance")
def reconcile(app_id: str | None = None, job_id: str | None = None) -> dict[str, Any]:
    """PG 与 Qdrant 对账（beat 每日；手动执行时带 job_id）。"""
    return asyncio.run(
        run_reconcile_job(UUID(app_id) if app_id else None, UUID(job_id) if job_id else None)
    )


@celery_app.task(name="aics.sweep_knowledge", queue="maintenance")
def sweep_knowledge() -> dict[str, int]:
    """每小时兜底：超时的删除、遗留旧版本、Qdrant 孤儿点。"""
    return asyncio.run(run_sweep_job())


@celery_app.task(name=IDLE_SWEEP_TASK, queue="maintenance")
def idle_sweep() -> dict[str, Any]:
    """Every 30 s (beat): idle notice and idle close (会话生命周期 §2.2); idempotent."""
    return asyncio.run(run_idle_sweep_job())


@celery_app.task(name=KEY_USAGE_FLUSH_TASK, queue="maintenance")
def flush_key_usage() -> dict[str, int]:
    """Every 60 s (beat): fold the key-usage buckets into `api_keys.last_used_at` (M3.1)."""
    return asyncio.run(run_key_usage_flush_job())
