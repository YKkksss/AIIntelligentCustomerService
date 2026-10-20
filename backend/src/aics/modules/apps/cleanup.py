"""应用删除的清理任务（M4.2 ③）：`aics.cleanup_app`。

`DELETE /apps/{id}` 只把应用置 `deleting` 并投递本任务；真正的数据删除在这里，顺序是
向量点 → 文件 → 数据库行 → `apps` 行。每一步都幂等，**可以重复执行**（ISSUE-76）：应用行
已经不在就直接返回，任务行已经 `done` 也不再动数据。

与其它 maintenance 任务一样自建引擎（`NullPool`），任务结束释放。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from aics.core.config import Settings, get_settings
from aics.db.models import (
    ApiKey,
    App,
    Chunk,
    ChunkParent,
    ChunkQuestion,
    Conversation,
    ConversationEvent,
    ConversationFact,
    ConversationFactHistory,
    Document,
    Feedback,
    Job,
    Message,
    UserAppRole,
    WebhookDelivery,
)
from aics.storage import build_storage
from aics.vectorstore import get_vector_store

log = structlog.get_logger()

CLEANUP_TASK = "aics.cleanup_app"

# 先子表后父表：没有级联外键的表（messages / chunks / api_keys 的 app_id）必须先删。
_TABLES: tuple[Any, ...] = (
    WebhookDelivery,
    ConversationEvent,
    Message,
    ConversationFactHistory,
    ConversationFact,
    Conversation,
    ChunkQuestion,
    ChunkParent,
    Chunk,
    Document,
    Feedback,
    ApiKey,
    UserAppRole,
)


def _now() -> datetime:
    return datetime.now(UTC)


async def run_cleanup_app_job(
    app_id: UUID, job_id: UUID | None = None, settings: Settings | None = None
) -> dict[str, Any]:
    """删除一个应用的全部数据；返回各表删除行数，可重复执行。"""
    s = settings or get_settings()
    engine = create_async_engine(s.db_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    result: dict[str, Any] = {"app_id": str(app_id), "status": "done", "deleted": {}}
    try:
        async with sessions() as session:
            app = await session.get(App, app_id)
            job = await session.get(Job, job_id) if job_id is not None else None
            if job is not None and job.status == "done":
                result["status"] = "already_done"
                return result
            if app is None:
                await _finish(session, job, "done", {"reason": "already_cleaned"})
                await session.commit()
                result["status"] = "missing"
                return result
            if job is not None:
                job.status, job.started_at, job.progress, job.error = "running", _now(), 10, None
                await session.commit()

            result["vectors"] = await _delete_vectors(app_id)
            result["files"] = await _delete_files(s, app_id)
            deleted: dict[str, int] = {}
            for model in _TABLES:
                stmt = delete(model).where(model.app_id == app_id)
                deleted[model.__tablename__] = int(
                    (await session.execute(stmt)).rowcount or 0  # type: ignore[attr-defined]
                )
            # 任务行保留成平台级记录（jobs.app_id 可空），审计里仍能追到是谁删的
            if job is not None:
                job.app_id = None
                await session.flush()
            jobs_deleted = int(
                (await session.execute(delete(Job).where(Job.app_id == app_id))).rowcount or 0  # type: ignore[attr-defined]
            )
            deleted["jobs"] = jobs_deleted
            await session.delete(app)
            await _finish(session, job, "done", {"deleted": deleted})
            await session.commit()
            result["deleted"] = deleted
            log.info("apps.cleanup_done", app_id=str(app_id), **{k: deleted[k] for k in deleted})
            return result
    except Exception as e:  # noqa: BLE001 - 失败要记进任务行，供运维重跑
        log.exception("apps.cleanup_failed", app_id=str(app_id))
        result["status"] = "failed"
        result["error"] = f"{type(e).__name__}: {e}"
        if job_id is not None:
            await _mark_failed(sessions, job_id, result["error"])
        return result
    finally:
        await engine.dispose()


async def _delete_vectors(app_id: UUID) -> str:
    try:
        await get_vector_store().delete_app(app_id)
        return "done"
    except Exception as e:  # noqa: BLE001 - Qdrant 不可用时先删库，孤儿点由 reconcile 兜底
        log.warning("apps.cleanup_vectors_failed", app_id=str(app_id), error_type=type(e).__name__)
        return f"skipped: {type(e).__name__}"


async def _delete_files(s: Settings, app_id: UUID) -> int:
    try:
        return await build_storage(s).delete_prefix(f"{app_id}/")
    except Exception as e:  # noqa: BLE001 - 文件删不掉不阻塞；孤儿文件由运维清理
        log.warning("apps.cleanup_files_failed", app_id=str(app_id), error_type=type(e).__name__)
        return 0


async def _finish(session: Any, job: Job | None, status: str, payload: dict[str, Any]) -> None:
    if job is None:
        return
    job.status, job.progress, job.finished_at = status, 100, _now()
    job.payload = {**(job.payload or {}), "result": payload}


async def _mark_failed(sessions: Any, job_id: UUID, error: str) -> None:
    try:
        async with sessions() as session:
            job = await session.get(Job, job_id)
            if job is not None:
                job.status, job.error, job.finished_at = "failed", error[:500], _now()
                await session.commit()
    except Exception:  # noqa: BLE001 - 兜底失败只能留日志
        log.exception("apps.cleanup_mark_failed", job_id=str(job_id))


__all__: Sequence[str] = ("CLEANUP_TASK", "run_cleanup_app_job")
