"""M4.3 里程碑验收：管理版知识库（上传 → 入库 → 切块 → 预览 → 复核 → 删除）。

真实 HTTP + 真 PG/Redis/Qdrant + 本地文件存储；嵌入器用进程内确定性假实现，**不调用模型**。
步骤：

    ① `aics-cli bootstrap-admin` 建管理员并登录（复用 M4.1 入口）
    ② 上传 Markdown（multipart + `chunk_config`）→ 202 + `job_id`，文档停在 `queued`；
       非法 `chunk_config` → 422 且 `loc` 指到字段
    ③ 进程内跑入库作业（`ingest_runtime(embedder=假嵌入器)`）→ 文档 `done`、切块数 > 0
    ④ 文档列表 `q` 命中、详情切块数与列表一致、切块列表带 `heading_path`
    ⑤ 上传前预览：`total_chunks` > 0 且不落库；`chunk_size` 变小则块数变多
    ⑥ 切块启停与复核：禁用 → `enabled=false`；复核通过 → `review_status=approved` + `reviewed_at`
    ⑦ 待复核队列：库内把某切块置 `flagged` → `GET /chunks?review_status=flagged` 可见 →
       复核通过后从该队列消失
    ⑧ 删除：`DELETE` → 202 + `deleting`；跑清理作业 → 详情 404、文档与切块行清零
    ⑨ 审计：`kb.document.create` / `kb.document.delete` / `kb.chunk.update` 均落 `audit_logs`
    ⑩ 跨应用隔离：另一个应用访问本应用文档 → 404

用法（在 `backend/` 下，需本机 Docker 栈）：

    uv run --frozen alembic upgrade head
    uv run --frozen python -m scripts.e2e_m4.e2e_m43_knowledge
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import uvicorn
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aics.core.config import get_settings
from aics.db.models import ApiKey, App, AuditLog, Chunk, Document, Job, User, UserAppRole
from aics.ingest.service import ingest_runtime, run_cleanup_job
from aics.main import create_app

RESULTS = Path(__file__).resolve().parent / "results"
BACKEND = Path(__file__).resolve().parents[2]

MD = (
    "# 售后政策\n\n## 退货\n\n"
    + "签收后七天内可无理由退货，运费由责任方承担。生鲜与定制商品除外。\n\n" * 40
    + "## 换货\n\n七天至十五天内可换货，需保持包装完好。\n\n" * 10
).encode()
CHUNK_CONFIG = {"chunk_size": 300, "chunk_overlap": 40}


class FakeEmbedder:
    """确定性嵌入器：只用于让入库流水线在没有模型 Key 的机器上跑通。"""

    def __init__(self, dim: int) -> None:
        self.model = "fake-acceptance"
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        raw = hashlib.sha256(text.encode()).digest()
        return [(raw[i % len(raw)] / 255) + 0.001 for i in range(self.dim)]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Transcript:
    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []
        self.checks: dict[str, bool] = {}

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks[name] = bool(ok)
        mark = "OK  " if ok else "FAIL"
        print(f"    {mark} {name}{'  ← ' + detail if detail else ''}")

    @property
    def passed(self) -> int:
        return sum(1 for ok in self.checks.values() if ok)

    def record(self, step: str, **body: Any) -> None:
        self.steps.append({"step": step, "body": body})


def bootstrap_admin(email: str, password: str) -> int:
    """走真正的 CLI（同时验证入口与幂等）。"""
    proc = subprocess.run(  # noqa: S603 —— 命令与参数都是本脚本写死的
        [
            sys.executable,
            "-m",
            "aics.cli",
            "bootstrap-admin",
            "--email",
            email,
            "--password",
            password,
            "--name",
            "验收管理员",
        ],
        cwd=str(BACKEND),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    print(f"    bootstrap-admin exit={proc.returncode} out={(proc.stdout or '').strip()[:120]}")
    return proc.returncode


def error_of(resp: httpx.Response) -> dict[str, Any]:
    body = resp.json()
    return body.get("error", {}) if isinstance(body, dict) else {}


async def _run_ingest(document_id: UUID, job_id: UUID, dim: int) -> str:
    async with ingest_runtime(embedder=FakeEmbedder(dim)) as svc:
        return await svc.run_build(document_id, job_id)


async def run() -> dict[str, Any]:
    s = get_settings()
    engine = create_async_engine(s.db_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    t = Transcript()
    stamp = datetime.now(UTC).strftime("%m%d%H%M%S")
    admin_email = f"admin-{stamp}@m43.example.com"
    admin_password = "Passw0rd-admin-1"  # noqa: S105 —— 一次性的验收账号
    app_name = f"验收知识库-{stamp}"
    other_name = f"验收对照-{stamp}"
    created_apps: list[UUID] = []

    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(), host="127.0.0.1", port=port, access_log=False, log_level="warning"
        )
    )
    task = asyncio.create_task(server.serve())
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                async with httpx.AsyncClient(timeout=2) as probe:
                    if (await probe.get(f"{base}/healthz")).status_code == 200:
                        break
            except httpx.HTTPError:
                await asyncio.sleep(0.1)

        async with httpx.AsyncClient(base_url=base, timeout=60) as http:
            print("① bootstrap-admin 与登录")
            code = await asyncio.to_thread(bootstrap_admin, admin_email, admin_password)
            t.check("bootstrap-admin 退出码 0", code == 0, f"exit={code}")
            login = await http.post(
                "/api/v1/admin/auth/login",
                json={"email": admin_email, "password": admin_password},
            )
            t.check("管理员登录 200", login.status_code == 200, str(login.status_code))
            auth = {"Authorization": f"Bearer {login.json()['access_token']}"}

            created = await http.post("/api/v1/admin/apps", headers=auth, json={"name": app_name})
            app_id = UUID(created.json()["id"])
            created_apps.append(app_id)
            other = await http.post("/api/v1/admin/apps", headers=auth, json={"name": other_name})
            other_app = UUID(other.json()["id"])
            created_apps.append(other_app)
            base_path = f"/api/v1/admin/apps/{app_id}"

            print("② 上传文档与切块配置校验")
            upload = await http.post(
                f"{base_path}/documents",
                headers=auth,
                files={"file": ("m43-policy.md", MD, "text/markdown")},
                data={"title": "M4.3 验收文档", "chunk_config": json.dumps(CHUNK_CONFIG)},
            )
            body = upload.json()
            t.record("upload", status=upload.status_code, body=body)
            t.check("上传 202", upload.status_code == 202, str(upload.status_code))
            document_id = UUID(body["id"])
            job_id = UUID(body["job_id"]) if body.get("job_id") else None
            t.check("上传返回 job_id", job_id is not None, str(body.get("job_id")))
            t.check("上传后文档 queued", body.get("status") == "queued", str(body.get("status")))

            broken = await http.post(
                f"{base_path}/documents",
                headers=auth,
                files={"file": ("m43-bad.md", MD, "text/markdown")},
                data={"chunk_config": json.dumps({"chunk_size": 1})},
            )
            loc = error_of(broken).get("details", {}).get("errors", [{}])[0].get("loc")
            t.check(
                "非法切块配置 422 且 loc 指到字段",
                broken.status_code == 422 and (loc or [])[:2] == ["chunk_config", "chunk_size"],
                f"{broken.status_code}/{loc}",
            )

            print("③ 进程内跑入库作业")
            if job_id is None:
                raise RuntimeError("上传未返回 job_id，后续步骤无法执行")
            result = await _run_ingest(document_id, job_id, s.embed_dim)
            t.check("入库作业成功", result == "done", str(result))
            detail = await http.get(f"{base_path}/documents/{document_id}", headers=auth)
            doc = detail.json()
            t.check("文档状态 done", doc.get("status") == "done", str(doc.get("status")))
            t.check("切块数 > 0", int(doc.get("chunk_count") or 0) > 0, str(doc.get("chunk_count")))
            async with sessions() as session:
                stored = await session.get(Document, document_id)
                stored_config = dict(stored.chunk_config or {}) if stored is not None else {}
            t.check(
                "改写成自定义切块配置",
                stored_config.get("chunk_size") == 300,
                str(stored_config),
            )

            print("④ 列表 / 详情 / 切块")
            listed = await http.get(f"{base_path}/documents", headers=auth, params={"q": "验收"})
            items = listed.json()["items"]
            t.check(
                "列表按标题命中",
                listed.status_code == 200 and [i["id"] for i in items] == [str(document_id)],
                str(len(items)),
            )
            chunks = await http.get(f"{base_path}/documents/{document_id}/chunks", headers=auth)
            chunk_items = chunks.json()["items"]
            t.check(
                "切块列表 200 且非空",
                chunks.status_code == 200 and chunk_items,
                str(len(chunk_items)),
            )
            t.check(
                "切块带 heading_path 与 tokens",
                all("heading_path" in c and "tokens" in c for c in chunk_items),
                str(sorted(chunk_items[0])),
            )
            jobs = await http.get(
                f"{base_path}/jobs", headers=auth, params={"type": "ingest", "page_size": 5}
            )
            t.check(
                "任务列表含 ingest",
                jobs.status_code == 200
                and any(j["target_id"] == str(document_id) for j in jobs.json()["items"]),
                str(jobs.json().get("total")),
            )

            print("⑤ 上传前预览")
            docs_before = (await http.get(f"{base_path}/documents", headers=auth)).json()["total"]
            small = await http.post(
                f"{base_path}/documents/preview",
                headers=auth,
                files={"file": ("m43-policy.md", MD, "text/markdown")},
                data={"chunk_config": json.dumps({"chunk_size": 200, "chunk_overlap": 20})},
            )
            large = await http.post(
                f"{base_path}/documents/preview",
                headers=auth,
                files={"file": ("m43-policy.md", MD, "text/markdown")},
                data={"chunk_config": json.dumps({"chunk_size": 2000, "chunk_overlap": 0})},
            )
            small_body = small.json()
            large_body = large.json()
            t.check(
                "预览 200 且 total_chunks > 0",
                small.status_code == 200 and int(small_body.get("total_chunks") or 0) > 0,
                str(small_body.get("total_chunks")),
            )
            t.check(
                "预览块数随 chunk_size 变小而变多",
                int(small_body["total_chunks"]) > int(large_body["total_chunks"]),
                f"{small_body.get('total_chunks')} vs {large_body.get('total_chunks')}",
            )
            docs_after = (await http.get(f"{base_path}/documents", headers=auth)).json()["total"]
            t.check("预览不落库", docs_before == docs_after, f"{docs_before} → {docs_after}")

            print("⑥ 切块启停与复核")
            first = chunk_items[0]
            disabled = await http.patch(
                f"{base_path}/chunks/{first['id']}", headers=auth, json={"enabled": False}
            )
            t.check(
                "禁用切块 200 且 enabled=false",
                disabled.status_code == 200 and disabled.json()["enabled"] is False,
                str(disabled.status_code),
            )
            approved = await http.patch(
                f"{base_path}/chunks/{first['id']}",
                headers=auth,
                json={"review_status": "approved"},
            )
            approved_body = approved.json()
            t.check(
                "复核通过 200 且带复核人时间",
                approved.status_code == 200
                and approved_body["review_status"] == "approved"
                and bool(approved_body.get("reviewed_at")),
                str(approved_body.get("review_status")),
            )
            t.check(
                "复核通过同时恢复启用",
                approved_body["enabled"] is True,
                str(approved_body["enabled"]),
            )
            bad_review = await http.patch(
                f"{base_path}/chunks/{first['id']}", headers=auth, json={"review_status": "flagged"}
            )
            t.check(
                "不允许写回 flagged 422", bad_review.status_code == 422, str(bad_review.status_code)
            )
            empty_patch = await http.patch(
                f"{base_path}/chunks/{first['id']}", headers=auth, json={}
            )
            t.check("空补丁 422", empty_patch.status_code == 422, str(empty_patch.status_code))

            print("⑦ 待复核队列")
            flagged_id = UUID(chunk_items[1]["id"])
            async with sessions() as session:
                chunk = await session.get(Chunk, flagged_id)
                if chunk is None:
                    raise RuntimeError("切块未找到，无法构造待复核样本")
                chunk.review_status = "flagged"
                chunk.flag_reason = "验收：疑似页眉残留"
                chunk.enabled = False
                await session.commit()
            queue = await http.get(
                f"{base_path}/chunks", headers=auth, params={"review_status": "flagged"}
            )
            ids = [c["id"] for c in queue.json()["items"]]
            t.check("待复核列表可见", str(flagged_id) in ids, str(len(ids)))
            fixed = await http.patch(
                f"{base_path}/chunks/{flagged_id}",
                headers=auth,
                json={"review_status": "approved"},
            )
            t.check("复核通过 200", fixed.status_code == 200, str(fixed.status_code))
            queue_after = await http.get(
                f"{base_path}/chunks", headers=auth, params={"review_status": "flagged"}
            )
            t.check(
                "复核后离开待复核队列",
                str(flagged_id) not in [c["id"] for c in queue_after.json()["items"]],
            )

            print("⑧ 删除与清理")
            removed = await http.delete(f"{base_path}/documents/{document_id}", headers=auth)
            removed_body = removed.json()
            t.check("删除 202", removed.status_code == 202, str(removed.status_code))
            deleting = await http.get(f"{base_path}/documents/{document_id}", headers=auth)
            t.check(
                "删除后状态 deleting",
                deleting.status_code == 200 and deleting.json().get("status") == "deleting",
                str(deleting.json().get("status")),
            )
            cleanup_job = UUID(removed_body["job_id"])
            await run_cleanup_job(document_id, None, None, job_id=cleanup_job)
            gone = await http.get(f"{base_path}/documents/{document_id}", headers=auth)
            t.check("清理后详情 404", gone.status_code == 404, str(gone.status_code))
            async with sessions() as session:
                docs_left = await session.scalar(
                    select(func.count()).select_from(Document).where(Document.app_id == app_id)
                )
                chunks_left = await session.scalar(
                    select(func.count()).select_from(Chunk).where(Chunk.app_id == app_id)
                )
            t.check("文档行清零", docs_left == 0, str(docs_left))
            t.check("切块行清零", chunks_left == 0, str(chunks_left))

            print("⑨ 审计")
            async with sessions() as session:
                rows = await session.execute(
                    select(AuditLog.action).where(AuditLog.app_id == app_id)
                )
                actions = {r[0] for r in rows}
            t.check(
                "审计含 kb.document.create", "kb.document.create" in actions, str(sorted(actions))
            )
            t.check("审计含 kb.document.delete", "kb.document.delete" in actions)
            t.check("审计含 kb.chunk.update", "kb.chunk.update" in actions)

            print("⑩ 跨应用隔离")
            cross = await http.get(
                f"/api/v1/admin/apps/{other_app}/documents/{document_id}", headers=auth
            )
            cross_error = error_of(cross)
            t.check(
                "跨应用访问文档 404",
                cross.status_code == 404 and cross_error.get("code") == "DOCUMENT_NOT_FOUND",
                f"{cross.status_code}/{cross_error.get('code')}",
            )
            t.check(
                "跨应用 404 带 document_id",
                cross_error.get("details", {}).get("document_id") == str(document_id),
                str(cross_error.get("details")),
            )
    finally:
        server.should_exit = True
        await task
        async with sessions() as session:
            for app_id in created_apps:
                await session.execute(delete(ApiKey).where(ApiKey.app_id == app_id))
                await session.execute(delete(UserAppRole).where(UserAppRole.app_id == app_id))
                await session.execute(delete(Job).where(Job.app_id == app_id))
                await session.execute(delete(App).where(App.id == app_id))
            await session.execute(delete(User).where(User.email == admin_email))
            await session.commit()
        await engine.dispose()

    return {
        "passed": t.passed,
        "total": len(t.checks),
        "checks": t.checks,
        "steps": t.steps,
        "mode": "stub-embedding",
        "started_at": stamp,
    }


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    results = asyncio.run(run())
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / "e2e_m43_knowledge.json"
    path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    passed, total = results["passed"], results["total"]
    print(f"\n{passed}/{total} 项通过；结果写入 {path.relative_to(BACKEND)}")
    failed = [name for name, ok in results["checks"].items() if not ok]
    if failed:
        print("失败项：" + "；".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
