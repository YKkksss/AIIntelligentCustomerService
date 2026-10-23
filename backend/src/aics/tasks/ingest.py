"""入库任务（队列 ingest）：只调用 `aics.ingest.service`。"""

from __future__ import annotations

import asyncio
from uuid import UUID

from aics.ingest.service import run_ingest_job, run_retokenize_job
from aics.tasks.celery_app import celery_app

INGEST_TASK = "aics.ingest_document"
CLEANUP_TASK = "aics.cleanup_document"


def _enqueue_cleanup(
    document_id: UUID, below_index_version: int, old_storage_key: str | None
) -> None:
    celery_app.send_task(
        CLEANUP_TASK,
        args=[str(document_id), below_index_version, old_storage_key],
        queue="maintenance",
    )


@celery_app.task(name=INGEST_TASK, queue="ingest", soft_time_limit=1800, time_limit=2100)
def ingest_document(document_id: str, job_id: str) -> str:
    """构建文档索引；可重复执行（acks_late 重投时先清理同构建号的半成品）。"""
    return asyncio.run(
        run_ingest_job(UUID(document_id), UUID(job_id), enqueue_cleanup=_enqueue_cleanup)
    )


@celery_app.task(name="aics.retokenize_app", queue="ingest", soft_time_limit=1800, time_limit=2100)
def retokenize_app(app_id: str, job_id: str) -> str:
    """custom_dict 变化后只重新分词（重算 search_text），不重新向量化。"""
    return asyncio.run(run_retokenize_job(UUID(app_id), UUID(job_id)))
