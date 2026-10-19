"""`aics-cli` 入口。

- `reindex --app ID [--document ID] [--inline]`：建重建任务并投递 Celery（`--inline` 在本进程直接执行构建）。
- `reconcile [--app ID] [--inline]`：PG 与 Qdrant 对账；`--inline` 直接执行并输出 JSON，有差异时退出码 1。
- `apikey create --app ID --name N --scopes chat[,kb:read…] [--rpm 60] [--quota N] [--expires-days N]
  [--ip CIDR]…`：创建开放接口 Key，明文只在此输出一次；`apikey revoke --app ID --id KEY_ID`：吊销（M2.6）；
  `apikey allowip --app ID --id KEY_ID [--ip CIDR]…`：设置 / 清空 IP 白名单，不传 `--ip` 即清空（M3.1）。
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
    "eval": "M5：运行评测",
}


# L4 入口之间互不 import：按任务名经 broker 投递。队列映射的唯一来源是 knowledge 服务
# （`TASK_QUEUES`），与 tasks.celery_app 的 task_routes 保持一致。
def _task_queues() -> dict[str, str]:
    from aics.modules.knowledge.service import TASK_QUEUES

    return TASK_QUEUES


def _celery_enqueue(task: str, args: Sequence[Any]) -> None:
    from celery import Celery

    from aics.core.config import get_settings

    client = Celery("aics-cli", broker=get_settings().celery_broker_url)
    q = _task_queues()[task]
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


async def _apikey(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime, timedelta

    import redis.asyncio as aioredis

    from aics.core.config import get_settings
    from aics.db.engine import dispose_engine, get_sessionmaker
    from aics.modules.apikeys.service import ApiKeyService
    from aics.modules.apps.service import AppService

    s = get_settings()
    redis = aioredis.from_url(s.redis_url)
    try:
        async with get_sessionmaker()() as session:
            await AppService(session).get_app(args.app)  # 404 for unknown / deleting apps
            svc = ApiKeyService(
                session,
                pepper=s.api_key_pepper.get_secret_value(),
                redis=redis,
                redis_prefix=s.redis_prefix,
            )
            if args.apikey_cmd == "create":
                expires = (
                    datetime.now(UTC) + timedelta(days=args.expires_days)
                    if args.expires_days
                    else None
                )
                row, plain = await svc.create(
                    args.app,
                    name=args.name,
                    scopes=[x.strip() for x in args.scopes.split(",") if x.strip()],
                    rpm_limit=args.rpm,
                    daily_quota=args.quota,
                    expires_at=expires,
                    ip_allowlist=args.ip,
                )
                await session.commit()
                out = {"id": str(row.id), "prefix": row.prefix, "scopes": row.scopes, "key": plain}
                print(json.dumps(out, ensure_ascii=False))
                print("该 Key 只显示这一次，请妥善保存。", file=sys.stderr)
                return 0
            if args.apikey_cmd == "allowip":
                ok = await svc.set_ip_allowlist(args.app, args.id, args.ip)
                await session.commit()
                print(json.dumps({"id": str(args.id), "ip_allowlist": args.ip or None, "ok": ok}))
                return 0 if ok else 1
            ok = await svc.revoke(args.app, args.id)
            await session.commit()
            print(json.dumps({"id": str(args.id), "revoked": ok}))
            return 0 if ok else 1
    finally:
        await redis.aclose()
        await dispose_engine()


async def _bootstrap_admin(args: argparse.Namespace) -> int:
    from aics.core.config import get_settings
    from aics.db.engine import dispose_engine, get_sessionmaker
    from aics.modules.auth import passwords
    from aics.modules.auth.repository import AuthRepository

    s = get_settings()
    email = (args.email or s.bootstrap_admin_email or "").strip().lower()
    secret = args.password or (
        s.bootstrap_admin_password.get_secret_value() if s.bootstrap_admin_password else None
    )
    if not email or not secret:
        print(
            "缺少管理员凭据：设置 AICS_BOOTSTRAP_ADMIN_EMAIL / AICS_BOOTSTRAP_ADMIN_PASSWORD，"
            "或用 --email / --password 传入。",
            file=sys.stderr,
        )
        return 2
    try:
        async with get_sessionmaker()() as session:
            repo = AuthRepository(session)
            user = await repo.user_by_email(email)
            if user is None:
                user = await repo.create_user(
                    email=email,
                    display_name=args.name or "管理员",
                    password_hash=passwords.hash_password(secret),
                )
                user.must_change_password = False
                user.is_super_admin = True
                await session.commit()
                print(json.dumps({"email": user.email, "created": True, "id": str(user.id)}))
                return 0
            changed = False
            if not user.is_super_admin:
                user.is_super_admin = True
                changed = True
            if args.reset_password:
                user.password_hash = passwords.hash_password(secret)
                user.must_change_password = False
                user.token_version = (user.token_version or 0) + 1
                changed = True
            if changed:
                await session.commit()
            print(
                json.dumps(
                    {
                        "email": user.email,
                        "created": False,
                        "id": str(user.id),
                        "promoted": bool(user.is_super_admin),
                        "password_reset": bool(args.reset_password),
                    }
                )
            )
            return 0
    finally:
        await dispose_engine()


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
    p = sub.add_parser("apikey", help="开放接口 API Key（创建 / 吊销）")
    ak = p.add_subparsers(dest="apikey_cmd", required=True)
    c = ak.add_parser("create", help="创建 Key（明文只输出一次）")
    c.add_argument("--app", required=True, type=UUID)
    c.add_argument("--name", required=True)
    c.add_argument("--scopes", default="chat", help="逗号分隔：chat,kb:read,kb:write,handoff:write")
    c.add_argument("--rpm", type=int, default=60)
    c.add_argument("--quota", type=int, default=None)
    c.add_argument("--expires-days", type=int, default=None)
    c.add_argument(
        "--ip",
        action="append",
        default=None,
        metavar="CIDR",
        help="IP 白名单，可重复（如 --ip 10.0.0.0/8 --ip 2001:db8::/32）；不传 = 不限制",
    )
    r = ak.add_parser("revoke", help="吊销 Key")
    r.add_argument("--app", required=True, type=UUID)
    r.add_argument("--id", required=True, type=UUID)
    ai = ak.add_parser("allowip", help="设置 / 清空 Key 的 IP 白名单（立即生效）")
    ai.add_argument("--app", required=True, type=UUID)
    ai.add_argument("--id", required=True, type=UUID)
    ai.add_argument(
        "--ip", action="append", default=None, metavar="CIDR", help="不传 = 清空（不限制）"
    )
    sub.add_parser("sweep-idle", help="立即执行一次空闲提醒 / 关闭扫描（本进程）")
    p = sub.add_parser("bootstrap-admin", help="创建 / 提升初始超级管理员（幂等）")
    p.add_argument("--email", help="覆盖 AICS_BOOTSTRAP_ADMIN_EMAIL")
    p.add_argument("--password", help="覆盖 AICS_BOOTSTRAP_ADMIN_PASSWORD")
    p.add_argument("--name", help="显示名（仅首次创建时生效）")
    p.add_argument(
        "--reset-password", action="store_true", help="已存在的账号也重置密码并踢掉旧会话"
    )
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
    if args.cmd == "apikey":
        return asyncio.run(_apikey(args))
    if args.cmd == "bootstrap-admin":
        return asyncio.run(_bootstrap_admin(args))
    if args.cmd == "sweep-idle":
        from aics.modules.chat.lifecycle import run_idle_sweep_job
        from aics.modules.handoff.sweep import run_handoff_sweep_job

        # 与 beat 的 idle_sweep 同序（会话生命周期与转人工 §3.10）：转人工超时在前
        print(
            json.dumps(
                {
                    "handoff": asyncio.run(run_handoff_sweep_job()),
                    "lifecycle": asyncio.run(run_idle_sweep_job()),
                }
            )
        )
        return 0
    print(f"{args.cmd}: 尚未实现（{PLANNED[args.cmd]}）", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
