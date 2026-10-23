"""应用生命周期任务（队列 maintenance）：只调 service（M4.2 ③）。"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from aics.modules.apps.cleanup import run_cleanup_app_job
from aics.tasks.celery_app import celery_app


@celery_app.task(
    name="aics.cleanup_app", queue="maintenance", soft_time_limit=1800, time_limit=2100
)
def cleanup_app(app_id: str, job_id: str | None = None) -> dict[str, Any]:
    """删除应用的全部数据；幂等，可重跑（ISSUE-76）。"""
    return asyncio.run(run_cleanup_app_job(UUID(app_id), UUID(job_id) if job_id else None))
