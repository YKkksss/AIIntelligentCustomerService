"""维护任务（队列 maintenance）：只调用 service。"""

from __future__ import annotations

import asyncio
from uuid import UUID

from aics.ingest.service import run_cleanup_job
from aics.tasks.celery_app import celery_app


@celery_app.task(name="aics.cleanup_document", queue="maintenance")
def cleanup_document(
    document_id: str, below_index_version: int | None = None, old_storage_key: str | None = None
) -> str:
    """删除旧版本的点 / 切块 / 父块 / 旧文件；文档 deleting 时删除全部并删文档行。"""
    return asyncio.run(run_cleanup_job(UUID(document_id), below_index_version, old_storage_key))
