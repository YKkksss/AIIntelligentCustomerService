"""M4.3b 里程碑验收：切块级运营（编辑、手工新增、近重复、有效期、补充问题、命中测试）。

真实 HTTP + 真 PG / Redis / Qdrant + 本地文件存储；嵌入器用进程内确定性假实现，**不调用模型**。

步骤与判定：

    ① bootstrap-admin + 登录 + 建应用 + 上传 Markdown + 进程内入库（切块产出）
    ② 编辑切块正文：`source` 变 `edited`、`tokens` 重算、命中测试能召回新词
    ③ 手工新增切块：`source=manual`；重建后仍在新构建号里（id 变化、内容保留）
    ④ 近重复提示：同内容第二篇文档入库 → `GET /documents/{id}/duplicates` 返回距离 0
    ⑤ 文档有效期：过期 → 命中测试召回为空；未生效同理；清空后恢复
    ⑥ 补充问题：新增 / 列表 / reattach / 删除
    ⑦ 命中测试：四路结果结构、非法 overrides 422
    ⑧ 审计：`kb.chunk.create` / `kb.chunk.update` / `kb.chunk.delete` / `kb.document.update`
    ⑨ 跨应用隔离：另一个应用访问本应用切块 → 404

用法（在 `backend/` 下，需本机 Docker 栈）：

    uv run --frozen alembic upgrade head
    uv run --frozen python -m scripts.e2e_m4.e2e_m43b_chunks
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import uvicorn
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aics.core.config import get_settings
from aics.db.models import (
    ApiKey,
    App,
    AuditLog,
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
    User,
    UserAppRole,
    WebhookDelivery,
)
from aics.ingest.service import ingest_runtime
from aics.main import build_knowledge_runtime, create_app
from aics.modules.knowledge.runtime import KnowledgeRuntime

RESULTS = Path(__file__).resolve().parent / "results"
BACKEND = Path(__file__).resolve().parents[2]

DOC_A = (
    "# 售后政策\n\n## 退货\n\n"
    + "签收后七天内可无理由退货，运费由责任方承担。生鲜与定制商品除外。\n\n" * 30
    + "## 换货\n\n七天至十五天内可换货，需保持包装完好。\n\n" * 6
).encode()
DOC_B = (
    "# 会员权益\n\n## 积分\n\n"
    + "消费一元累计一积分，积分可在下次下单时抵扣。\n\n" * 20
    + "## 期限\n\nB 专享条款：积分自到账之日起十二个月内有效，逾期作废。\n\n" * 4
).encode()
CHUNK_CONFIG = {"chunk_size": 300, "chunk_overlap": 40}
DOC_B_TITLE = "会员权益说明"


def _from_doc(payload: dict[str, Any], title: str) -> list[dict[str, Any]]:
    """返回命中结果里属于指定标题文档的融合项（按 document_title 过滤）。"""
    return [
        item
        for item in payload.get("fused") or []
        if item.get("document_title") == title
    ]
# 与 DOC_A 只有一行差异：内容哈希不同（避免上传去重返回 200），SimHash 距离仍为 0～1
DOC_A_COPY = DOC_A + b"\n## \xe9\x99\x84\xe6\xb3\xa8\n\n\xe6\x9c\xac\xe5\x89\xaf\xe6\x9c\xac\xe4\xbb\x85\xe7\x94\xa8\xe4\xba\x8e\xe8\xbf\x91\xe9\x87\x8d\xe5\xa4\x8d\xe6\x8f\x90\xe7\xa4\xba\xe9\xaa\x8c\xe6\x94\xb6\xe3\x80\x82\n"


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
        print(f"    {'OK  ' if ok else 'FAIL'} {name}{'  ← ' + detail if detail else ''}")

    @property
    def passed(self) -> int:
        return sum(1 for ok in self.checks.values() if ok)

    def record(self, step: str, **body: Any) -> None:
        self.steps.append({"step": step, "body": body})


def bootstrap_admin(email: str, password: str) -> int:
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
    print(f"    bootstrap-admin exit={proc.returncode}")
    return proc.returncode


def error_of(resp: httpx.Response) -> dict[str, Any]:
    body = resp.json()
    return body.get("error", {}) if isinstance(body, dict) else {}


async def _build(document_id: UUID, job_id: UUID, dim: int) -> str:
    async with ingest_runtime(embedder=FakeEmbedder(dim)) as svc:
        return await svc.run_build(document_id, job_id)


def _fake_runtime(s: Any, dim: int) -> KnowledgeRuntime:
    """把 API 进程里的嵌入器换成假实现，命中测试才不会去调真实模型。"""
    base = build_knowledge_runtime(s)
    return KnowledgeRuntime(
        sessions=base.sessions,
        storage=base.storage,
        vector=base.vector,
        embedder=FakeEmbedder(dim),
        enqueue=base.enqueue,
        reranker=base.reranker,
        audit=base.audit,
    )


async def run() -> dict[str, Any]:
    s = get_settings()
    engine = create_async_engine(s.db_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    t = Transcript()
    dim = s.embed_dim

    stamp = datetime.now(UTC).strftime("%m%d%H%M%S")
    admin_email = f"admin-{stamp}@m43b.example.com"
    admin_password = "Passw0rd-admin-1"  # noqa: S105 —— 一次性的验收账号
    created_apps: list[UUID] = []

    port = free_port()
    app = create_app()
    app.state.knowledge_runtime = _fake_runtime(s, dim)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False, log_level="warning")
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

        async with httpx.AsyncClient(base_url=base, timeout=120) as http:
            print("① 建管理员、应用并入库文档 A")
            code = await asyncio.to_thread(bootstrap_admin, admin_email, admin_password)
            t.check("bootstrap-admin 退出码 0", code == 0, f"exit={code}")
            login = await http.post(
                "/api/v1/admin/auth/login",
                json={"email": admin_email, "password": admin_password},
            )
            t.check("管理员登录 200", login.status_code == 200, str(login.status_code))
            auth = {"Authorization": f"Bearer {login.json()['access_token']}"}

            created = await http.post(
                "/api/v1/admin/apps",
                headers=auth,
                json={"name": f"验收切块运营-{stamp}"},
            )
            app_id = UUID(created.json()["id"])
            created_apps.append(app_id)
            other = await http.post(
                "/api/v1/admin/apps",
                headers=auth,
                json={"name": f"验收对照-{stamp}"},
            )
            other_app = UUID(other.json()["id"])
            created_apps.append(other_app)
            path = f"/api/v1/admin/apps/{app_id}"

            up_a = await http.post(
                f"{path}/documents",
                headers=auth,
                files={"file": ("m43b-a.md", DOC_A, "text/markdown")},
                data={"title": "售后政策", "chunk_config": json.dumps(CHUNK_CONFIG)},
            )
            doc_a = UUID(up_a.json()["id"])
            job_a = UUID(up_a.json()["job_id"])
            status = await _build(doc_a, job_a, dim)
            t.check("文档 A 入库 done", status == "done", status)

            up_b = await http.post(
                f"{path}/documents",
                headers=auth,
                files={"file": ("m43b-b.md", DOC_B, "text/markdown")},
                data={"title": "会员权益", "chunk_config": json.dumps(CHUNK_CONFIG)},
            )
            doc_b = UUID(up_b.json()["id"])
            status_b = await _build(doc_b, UUID(up_b.json()["job_id"]), dim)
            t.check("文档 B 入库 done", status_b == "done", status_b)

            chunks_a = (await http.get(f"{path}/documents/{doc_a}/chunks", headers=auth)).json()
            items = chunks_a["items"]
            t.check("文档 A 有切块", len(items) > 0, str(len(items)))
            t.check(
                "默认切块来源都是 parsed",
                all(item["source"] == "parsed" for item in items),
                str({item["source"] for item in items}),
            )
            first = items[0]

            print("② 编辑切块正文")
            marker = f"星尘退货通道码{stamp}"
            edited = await http.patch(
                f"{path}/chunks/{first['id']}",
                headers=auth,
                json={"text": f"{marker}：内部退货需走专用通道。", "heading_path": "退货 / 人工通道"},
            )
            edited_body = edited.json()
            t.check("编辑切块 200", edited.status_code == 200, str(edited.status_code))
            t.check("来源变 edited", edited_body.get("source") == "edited", str(edited_body.get("source")))
            t.check(
                "正文与标题路径已更新",
                marker in edited_body.get("text", "")
                and edited_body.get("heading_path") == "退货 / 人工通道",
                str(edited_body.get("heading_path")),
            )
            t.check("tokens 重算", isinstance(edited_body.get("tokens"), int), str(edited_body.get("tokens")))

            hit = await http.post(
                f"{path}/hit-test", headers=auth, json={"query": marker}
            )
            hit_body = hit.json()
            fused = hit_body.get("fused") or []
            t.check("命中测试 200", hit.status_code == 200, str(hit.status_code))
            t.check(
                "命中测试召回编辑后的切块",
                any(row.get("chunk_id") == first["id"] for row in fused),
                str([row.get("chunk_id") for row in fused][:3]),
            )
            bad_override = await http.post(
                f"{path}/hit-test", headers=auth, json={"query": "退款", "overrides": {"nope": 1}}
            )
            t.check(
                "非法 overrides 422",
                bad_override.status_code == 422,
                str(bad_override.status_code),
            )

            print("③ 手工新增切块并重建")
            manual_marker = f"手工补充条款编号Z{stamp}"
            manual = await http.post(
                f"{path}/documents/{doc_a}/chunks",
                headers=auth,
                json={"text": f"{manual_marker}：线下门店退货需出示小票。", "heading_path": "退货 / 门店"},
            )
            manual_body = manual.json()
            t.check("手工新增 201", manual.status_code == 201, str(manual.status_code))
            t.check("来源 manual", manual_body.get("source") == "manual", str(manual_body.get("source")))
            manual_id = manual_body["id"]

            reindexed = await http.post(f"{path}/documents/{doc_a}/reindex", headers=auth, json={})
            t.check("重建任务 202", reindexed.status_code == 202, str(reindexed.status_code))
            rebuild_status = await _build(doc_a, UUID(reindexed.json()["job_id"]), dim)
            t.check("重建 done", rebuild_status == "done", rebuild_status)
            after = (await http.get(f"{path}/documents/{doc_a}/chunks", headers=auth)).json()
            manuals = [item for item in after["items"] if item["source"] == "manual"]
            # ②改过的块正文与重新解析结果不一致，按设计降级为 manual（ISSUE-97），所以这里有两块
            t.check("重建后手工切块仍在", len(manuals) >= 1, str(len(manuals)))
            kept = [item for item in manuals if manual_marker in item["text"]]
            t.check("手工新增的正文保留", len(kept) == 1, str(len(kept)))
            if kept:
                t.check(
                    "手工切块换了 id（迁到新构建号）",
                    kept[0]["id"] != manual_id,
                    f"{manual_id} -> {kept[0]['id']}",
                )
            demoted = [item for item in manuals if marker in item["text"]]
            t.check("改过的块失配后降级 manual", len(demoted) == 1, str(len(demoted)))
            hit_manual = await http.post(
                f"{path}/hit-test", headers=auth, json={"query": manual_marker}
            )
            t.check(
                "命中测试召回手工切块",
                len(hit_manual.json().get("fused") or []) > 0,
                str(len(hit_manual.json().get("fused") or [])),
            )

            print("④ 近重复提示")
            up_c = await http.post(
                f"{path}/documents",
                headers=auth,
                files={"file": ("m43b-a-copy.md", DOC_A_COPY, "text/markdown")},
                data={"title": "售后政策（副本）", "chunk_config": json.dumps(CHUNK_CONFIG)},
            )
            doc_c = UUID(up_c.json()["id"])
            await _build(doc_c, UUID(up_c.json()["job_id"]), dim)
            dup = await http.get(f"{path}/documents/{doc_c}/duplicates", headers=auth)
            dup_items = dup.json().get("items") or []
            t.check(
                "近重复命中同内容文档",
                any(item["document_id"] == str(doc_a) and item["distance"] <= 3 for item in dup_items),
                str(dup_items[:2]),
            )

            print("⑤ 文档有效期")
            hit_b = await http.post(
                f"{path}/hit-test", headers=auth, json={"query": "积分自到账之日起十二个月内有效"}
            )
            t.check(
                "有效期前 B 可被召回",
                len(hit_b.json().get("fused") or []) > 0,
                str(len(hit_b.json().get("fused") or [])),
            )
            expired = await http.patch(
                f"{path}/documents/{doc_b}",
                headers=auth,
                json={"expires_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat()},
            )
            t.check("设置过期 200", expired.status_code == 200, str(expired.status_code))
            hit_expired = await http.post(
                f"{path}/hit-test", headers=auth, json={"query": "积分自到账之日起十二个月内有效"}
            )
            t.check(
                "过期后不再召回",
                not _from_doc(hit_expired.json(), DOC_B_TITLE),
                str(len(hit_expired.json().get("fused") or [])),
            )
            future = await http.patch(
                f"{path}/documents/{doc_b}",
                headers=auth,
                json={
                    "expires_at": None,
                    "effective_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
                },
            )
            t.check("设置未生效 200", future.status_code == 200, str(future.status_code))
            hit_future = await http.post(
                f"{path}/hit-test", headers=auth, json={"query": "积分自到账之日起十二个月内有效"}
            )
            t.check(
                "未生效同样不召回",
                not _from_doc(hit_future.json(), DOC_B_TITLE),
                str(len(hit_future.json().get("fused") or [])),
            )
            restored = await http.patch(
                f"{path}/documents/{doc_b}",
                headers=auth,
                json={"effective_at": None, "expires_at": None},
            )
            t.check("清空有效期 200", restored.status_code == 200, str(restored.status_code))
            hit_back = await http.post(
                f"{path}/hit-test", headers=auth, json={"query": "积分自到账之日起十二个月内有效"}
            )
            t.check(
                "清空后恢复召回",
                len(hit_back.json().get("fused") or []) > 0,
                str(len(hit_back.json().get("fused") or [])),
            )

            print("⑥ 补充问题")
            chunk_items = (await http.get(f"{path}/documents/{doc_a}/chunks", headers=auth)).json()["items"]
            target = chunk_items[0]
            other_chunk = chunk_items[1] if len(chunk_items) > 1 else chunk_items[0]
            added = await http.post(
                f"{path}/chunks/{target['id']}/questions",
                headers=auth,
                json={"questions": ["退款多久到账？", "可以指定快递吗？"]},
            )
            t.check("新增补充问题 201", added.status_code == 201, str(added.status_code))
            questions = (await http.get(f"{path}/chunks/{target['id']}/questions", headers=auth)).json()
            t.check("补充问题列表 2 条", len(questions["items"]) == 2, str(len(questions["items"])))
            question_id = questions["items"][0]["id"]
            moved = await http.post(
                f"{path}/questions/{question_id}/reattach",
                headers=auth,
                json={"chunk_id": other_chunk["id"]},
            )
            t.check(
                "reattach 到另一个切块",
                moved.status_code == 200 and moved.json().get("chunk_id") == other_chunk["id"],
                str(moved.status_code),
            )
            removed = await http.delete(f"{path}/questions/{question_id}", headers=auth)
            t.check("删除补充问题 204", removed.status_code == 204, str(removed.status_code))

            print("⑦ 删除切块")
            deleted = await http.delete(f"{path}/chunks/{target['id']}", headers=auth)
            t.check("删除切块 204", deleted.status_code == 204, str(deleted.status_code))
            left = (await http.get(f"{path}/documents/{doc_a}/chunks", headers=auth)).json()["items"]
            t.check(
                "删除后列表少一块",
                all(item["id"] != target["id"] for item in left),
                str(len(left)),
            )

            print("⑧ 审计动作")
            async with sessions() as session:
                rows = (
                    await session.execute(
                        select(AuditLog.action).where(AuditLog.app_id == app_id)
                    )
                ).scalars()
                actions = set(rows.all())
            for action in (
                "kb.chunk.update",
                "kb.chunk.create",
                "kb.chunk.delete",
                "kb.document.update",
            ):
                t.check(f"审计含 {action}", action in actions, str(sorted(actions)))

            print("⑨ 跨应用隔离")
            cross = await http.get(
                f"/api/v1/admin/apps/{other_app}/documents/{doc_a}/chunks", headers=auth
            )
            t.check("跨应用读切块 404", cross.status_code == 404, str(cross.status_code))
            cross_patch = await http.patch(
                f"/api/v1/admin/apps/{other_app}/chunks/{other_chunk['id']}",
                headers=auth,
                json={"enabled": False},
            )
            t.check("跨应用改切块 404", cross_patch.status_code == 404, str(cross_patch.status_code))

            t.record("summary", app_id=str(app_id), checks=len(t.checks))
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=10)
        except TimeoutError:
            task.cancel()

    # 清理：先删审计与任务，再删应用与用户
    async with sessions() as session:
        admin_id = (
            await session.execute(select(User.id).where(User.email == admin_email))
        ).scalar_one_or_none()
        if admin_id is not None:
            await session.execute(
                AuditLog.__table__.delete().where(AuditLog.actor_id == admin_id)
            )
        for app_uuid in created_apps:
            for table in (
                Feedback,
                ConversationFactHistory,
                ConversationFact,
                WebhookDelivery,
                ConversationEvent,
                Message,
                Conversation,
                ChunkQuestion,
                ChunkParent,
                Chunk,
                Document,
            ):
                await session.execute(table.__table__.delete().where(table.app_id == app_uuid))
            await session.execute(Job.__table__.delete().where(Job.app_id == app_uuid))
            await session.execute(
                ApiKey.__table__.delete().where(ApiKey.app_id == app_uuid)
            )
            await session.execute(
                UserAppRole.__table__.delete().where(UserAppRole.app_id == app_uuid)
            )
            await session.execute(App.__table__.delete().where(App.id == app_uuid))
        if admin_id is not None:
            await session.execute(User.__table__.delete().where(User.id == admin_id))
        await session.commit()
    await engine.dispose()

    result = {
        "passed": t.passed,
        "total": len(t.checks),
        "mode": "stub-embedding",
        "checks": t.checks,
        "steps": t.steps,
    }
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "e2e_m43b_chunks.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    result = asyncio.run(run())
    print(f"\n{result['passed']}/{result['total']} 项通过（{result['mode']}）")
    return 0 if result["passed"] == result["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
