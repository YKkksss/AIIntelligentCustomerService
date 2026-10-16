"""Conversation context tasks (queue `context`, 对话上下文管理 §7.1): only call memory.service."""

from __future__ import annotations

import asyncio
from uuid import UUID

from aics.memory.compactor import CompactionUpstreamError, CompactParams
from aics.memory.service import COMPACT_TASK, run_compaction_job
from aics.tasks.celery_app import celery_app

COMPACT_MAX_RETRIES = 3  # §7.2: upstream timeout / rate limit, exponential backoff


@celery_app.task(
    name=COMPACT_TASK,
    queue="context",
    autoretry_for=(CompactionUpstreamError,),
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=COMPACT_MAX_RETRIES,
)
def compact_conversation(
    app_id: str, conversation_id: str, expected_version: int, params: dict
) -> str:
    """Rolling summary + fact extraction; idempotent via summary_version (stale versions are
    dropped as version_conflict). Final failure leaves messages / summary / facts unchanged."""
    return asyncio.run(
        run_compaction_job(UUID(app_id), UUID(conversation_id), expected_version, params)
    )


def enqueue_compaction(
    app_id: UUID, conversation_id: UUID, expected_version: int, params: CompactParams
) -> None:
    """`MemoryService(enqueue_compaction=...)` for online / debug runs."""
    celery_app.send_task(
        COMPACT_TASK,
        args=[str(app_id), str(conversation_id), expected_version, params.model_dump()],
        queue="context",
        routing_key="context",
    )
