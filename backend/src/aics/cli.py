"""`aics-cli` 入口。

- `reindex --app ID [--document ID] [--inline]`：建重建任务并投递 Celery（`--inline` 在本进程直接执行构建）。
- `reconcile [--app ID] [--inline]`：PG 与 Qdrant 对账；`--inline` 直接执行并输出 JSON，有差异时退出码 1。
- bootstrap-admin（M4）、eval（M5）随里程碑实现。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from typing import Any
from uuid import UUID

PLANNED = {
    "bootstrap-admin": "M4：读取 AICS_BOOTSTRAP_ADMIN_* 创建初始管理员",
    "eval": "M5：运行评测",
}


# L4 入口之间互不 import：按任务名经 broker 投递（队列与 tasks.celery_app 的 task_routes 一致）
TASK_QUEUES = {
    "aics.ingest_document": "ingest",
    "aics.retokenize_app": "ingest",
    "aics.cleanup_document": "maintenance",
    "aics.reconcile": "maintenance",
}


def _celery_enqueue(task: str, args: Sequence[Any]) -> None:
    from celery import Celery

    from aics.core.config import get_settings

    client = Celery("aics-cli", broker=get_settings().celery_broker_url)
    q = TASK_QUEUES[task]
    client.send_task(task, args=list(args), queue=q, routing_key=q, exchange="aics")


async def _reindex(app_id: UUID, document_id: UUID | None, inline: bool) -> int:
    from aics.db.engine import dispose_engine, get_sessionmaker
    from aics.ingest.service import ingest_runtime
    from aics.modules.knowledge.service import KnowledgeService
    from aics.storage import get_storage

    queued: list[tuple[str, list[Any]]] = []

    def collect(task: str, args: Sequence[Any]) -> None:
        queued.append((task, list(args)))

    try:
        async with get_sessionmaker()() as s:
            svc = KnowledgeService(
                s, storage=get_storage(), enqueue=collect if inline else _celery_enqueue
            )
            if document_id is not None:
                jobs = [await svc.rebuild_document(app_id, document_id)]
                skipped: list[UUID] = []
            else:
                r = await svc.rebuild_app(app_id)
                jobs, skipped = r.jobs, r.skipped
        print(json.dumps({"jobs": [str(j.id) for j in jobs], "skipped": [str(i) for i in skipped]}))
    finally:
        await dispose_engine()
    if not inline:
        return 0

    async def cleanup_now(doc: UUID, below: int, old: str | None) -> None:
        async with ingest_runtime() as rt:
            await rt.run_cleanup(doc, below_index_version=below, old_storage_key=old)

    failed = 0
    async with ingest_runtime(enqueue_cleanup=cleanup_now) as rt:
        for _task, (doc, job) in queued:
            status = await rt.run_build(UUID(doc), UUID(job))
            print(json.dumps({"document_id": doc, "job_id": job, "status": status}))
            failed += status != "done"
    return 1 if failed else 0


async def _reconcile(app_id: UUID | None, inline: bool) -> int:
    from aics.db.engine import dispose_engine, get_sessionmaker
    from aics.ingest.service import ingest_runtime
    from aics.modules.knowledge.service import KnowledgeService
    from aics.storage import get_storage

    try:
        async with get_sessionmaker()() as s:
            svc = KnowledgeService(
                s, storage=get_storage(), enqueue=None if inline else _celery_enqueue
            )
            job = await svc.request_reconcile(app_id)
    finally:
        await dispose_engine()
    if not inline:
        print(json.dumps({"job_id": str(job.id)}))
        return 0
    async with ingest_runtime() as rt:
        result = await rt.run_reconcile(app_id, job.id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["diff_count"] else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aics-cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("version", help="显示版本")
    p = sub.add_parser("reindex", help="重建应用（或单个文档）的索引")
    p.add_argument("--app", required=True, type=UUID)
    p.add_argument("--document", type=UUID)
    p.add_argument("--inline", action="store_true", help="不经 Celery，在本进程执行")
    p = sub.add_parser("reconcile", help="PG 与 Qdrant 对账")
    p.add_argument("--app", type=UUID)
    p.add_argument("--inline", action="store_true", help="不经 Celery，在本进程执行并输出结果")
    for name, help_ in PLANNED.items():
        sub.add_parser(name, help=help_)
    args = parser.parse_args(argv)
    if args.cmd == "version":
        from aics import __version__

        print(__version__)
        return 0
    if args.cmd == "reindex":
        return asyncio.run(_reindex(args.app, args.document, args.inline))
    if args.cmd == "reconcile":
        return asyncio.run(_reconcile(args.app, args.inline))
    print(f"{args.cmd}: 尚未实现（{PLANNED[args.cmd]}）", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
