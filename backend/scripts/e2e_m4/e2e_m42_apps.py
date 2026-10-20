"""M4.2 里程碑验收：应用管理与系统设置（列表、配置、乐观并发、复制、删除与清理）。

真实 HTTP + 真 PG/Redis，不调用模型。步骤：

    ① `aics-cli bootstrap-admin` 建初始管理员并登录（复用 M4.1 的入口）
    ② `POST /admin/apps` 建应用（201，默认配置）→ 详情带 `stats` → 列表可见
    ③ `PATCH /admin/apps/{id}` 改检索参数（带 `expected_config_version`）→ 200 且版本 +
    ④ 旧版本号再提交 → 409 `CONFIG_VERSION_CONFLICT`；非法值 → 422 且 `loc` 指到字段
    ⑤ `GET /admin/models` 与 `/admin/chunk-presets` 可读
    ⑥ 系统设置读写：默认值 → 改 → 读回 → 审计 `system.update`；非法值 422 且 `loc` 有前缀
    ⑦ 给应用塞一条会话与一把 Key → `POST /apps/{id}/copy` 只复制配置（副本无 Key、无会话）
    ⑧ `DELETE /apps/{id}` → 202 → 详情 404、列表里消失 → 跑 `cleanup_app` → 数据清空
    ⑨ 清理任务重跑幂等（`already_done`）

用法（在 `backend/` 下，需本机 Docker 栈）：

    uv run --frozen alembic upgrade head
    uv run --frozen python -m scripts.e2e_m4.e2e_m42_apps
"""

from __future__ import annotations

import asyncio
import json
import os
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
from aics.db.models import (
    ApiKey,
    App,
    Conversation,
    Document,
    Job,
    Message,
    SystemSetting,
    User,
    UserAppRole,
)
from aics.main import create_app
from aics.modules.apps.cleanup import run_cleanup_app_job
from aics.modules.chat.repository import ConversationRepository, MessageRepository
from aics.modules.chat.schemas import NewConversation, NewMessage

RESULTS = Path(__file__).resolve().parent / "results"
BACKEND = Path(__file__).resolve().parents[2]


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


async def run() -> dict[str, Any]:
    s = get_settings()
    engine = create_async_engine(s.db_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    t = Transcript()
    stamp = datetime.now(UTC).strftime("%m%d%H%M%S")
    admin_email = f"admin-{stamp}@m42.example.com"
    admin_password = "Passw0rd-admin-1"  # noqa: S105 —— 一次性的验收账号
    app_name = f"验收应用-{stamp}"
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

        async with httpx.AsyncClient(base_url=base, timeout=15) as http:
            print("① bootstrap-admin 与登录")
            code = await asyncio.to_thread(bootstrap_admin, admin_email, admin_password)
            t.check("bootstrap-admin 退出码 0", code == 0, f"exit={code}")
            await asyncio.to_thread(bootstrap_admin, admin_email, admin_password)
            login = await http.post(
                "/api/v1/admin/auth/login",
                json={"email": admin_email, "password": admin_password},
            )
            t.check("管理员登录 200", login.status_code == 200, str(login.status_code))
            auth = {"Authorization": f"Bearer {login.json()['access_token']}"}
            admin_id = UUID(login.json()["user"]["id"])

            print("② 应用列表 / 创建 / 详情")
            listed = await http.get("/api/v1/admin/apps", headers=auth)
            t.check(
                "应用列表 200 且带 items", listed.status_code == 200 and "items" in listed.json()
            )
            forbidden = await http.post("/api/v1/admin/apps", headers={}, json={"name": "x"})
            t.check(
                "未带 token 建应用 401", forbidden.status_code == 401, str(forbidden.status_code)
            )

            created = await http.post(
                "/api/v1/admin/apps",
                headers=auth,
                json={"name": app_name, "description": "M4.2 里程碑验收"},
            )
            body = created.json()
            t.record(
                "create_app",
                status=created.status_code,
                config_version=body.get("config_version"),
            )
            t.check("建应用 201", created.status_code == 201, str(created.status_code))
            app_id = UUID(body["id"])
            created_apps.append(app_id)
            t.check(
                "默认配置版本 1", body.get("config_version") == 1, str(body.get("config_version"))
            )
            t.check(
                "新建应用带生效会话配置",
                "effective_session_config" in body and "stats" in body,
                str(sorted(body)),
            )
            detail = await http.get(f"/api/v1/admin/apps/{app_id}", headers=auth)
            detail_body = detail.json()
            t.check(
                "详情 200 且文档数为 0",
                detail.status_code == 200 and detail_body["stats"]["documents"] == 0,
                str(detail_body.get("stats")),
            )
            after_create = await http.get("/api/v1/admin/apps", headers=auth)
            ids = [a["id"] for a in after_create.json()["items"]]
            t.check("新建应用出现在列表里", str(app_id) in ids)

            print("③ 改配置（乐观并发）")
            patched = await http.patch(
                f"/api/v1/admin/apps/{app_id}",
                headers=auth,
                json={"rag_config": {"fuse_top_k": 33}, "expected_config_version": 1},
            )
            patched_body = patched.json()
            t.record(
                "patch",
                status=patched.status_code,
                config_version=patched_body.get("config_version"),
            )
            t.check("PATCH 200", patched.status_code == 200, str(patched.status_code))
            t.check(
                "检索参数已写入",
                patched_body["rag_config"]["fuse_top_k"] == 33,
                str(patched_body["rag_config"].get("fuse_top_k")),
            )
            t.check(
                "配置版本递增到 2",
                patched_body["config_version"] == 2,
                str(patched_body["config_version"]),
            )

            stale = await http.patch(
                f"/api/v1/admin/apps/{app_id}",
                headers=auth,
                json={"greeting": "你好呀", "expected_config_version": 1},
            )
            stale_error = error_of(stale)
            t.check(
                "旧版本号 409 CONFIG_VERSION_CONFLICT",
                stale.status_code == 409 and stale_error.get("code") == "CONFIG_VERSION_CONFLICT",
                f"{stale.status_code}/{stale_error.get('code')}",
            )
            t.check(
                "409 带 expected / current",
                error_of(stale).get("details") == {"expected": 1, "current": 2},
                str(stale_error.get("details")),
            )

            invalid = await http.patch(
                f"/api/v1/admin/apps/{app_id}",
                headers=auth,
                json={"rag_config": {"fuse_top_k": 9999}, "expected_config_version": 2},
            )
            loc = error_of(invalid).get("details", {}).get("errors", [{}])[0].get("loc")
            t.check(
                "非法值 422 且 loc 指到字段",
                invalid.status_code == 422 and loc == ["rag_config", "fuse_top_k"],
                f"{invalid.status_code}/{loc}",
            )

            print("④ 模型列表与切块预设")
            models = await http.get("/api/v1/admin/models", headers=auth)
            presets = await http.get("/api/v1/admin/chunk-presets", headers=auth)
            t.check(
                "模型列表 200 且有对话模型",
                models.status_code == 200 and any(m["kind"] == "chat" for m in models.json()),
                str(sorted({m["kind"] for m in models.json()})),
            )
            t.check(
                "切块预设 200 且首项含 chunk_size",
                presets.status_code == 200 and "chunk_size" in presets.json()[0]["config"],
                str(sorted(presets.json()[0])),
            )

            print("⑤ 系统设置读写")
            settings_before = await http.get("/api/v1/admin/system/settings", headers=auth)
            before_cfg = settings_before.json()["default_session_config"]
            t.check(
                "系统设置 200 且含三项",
                settings_before.status_code == 200
                and {"idle_close_minutes", "idle_notice_before_minutes", "idle_close_notice"}
                <= set(before_cfg),
                str(sorted(before_cfg)),
            )
            changed = await http.patch(
                "/api/v1/admin/system/settings",
                headers=auth,
                json={"default_session_config": {"idle_close_minutes": 20}},
            )
            t.check(
                "改系统设置 200 且值生效",
                changed.status_code == 200
                and changed.json()["default_session_config"]["idle_close_minutes"] == 20,
                str(changed.status_code),
            )
            settings_after = await http.get("/api/v1/admin/system/settings", headers=auth)
            t.check(
                "再次读取仍是 20",
                settings_after.json()["default_session_config"]["idle_close_minutes"] == 20,
            )
            bad_setting = await http.patch(
                "/api/v1/admin/system/settings",
                headers=auth,
                json={"default_session_config": {"idle_close_minutes": 0}},
            )
            bad_loc = error_of(bad_setting).get("details", {}).get("errors", [{}])[0].get("loc")
            t.check(
                "非法系统设置 422 且 loc 带前缀",
                bad_setting.status_code == 422
                and bad_loc == ["default_session_config", "idle_close_minutes"],
                f"{bad_setting.status_code}/{bad_loc}",
            )
            restored = await http.patch(
                "/api/v1/admin/system/settings",
                headers=auth,
                json={"default_session_config": {"idle_close_minutes": 10}},
            )
            t.check("复原默认会话设置", restored.status_code == 200, str(restored.status_code))

            print("⑥ 复制应用（不带数据）")
            async with sessions() as sess:
                conv = await ConversationRepository(sess).create(
                    app_id, NewConversation(channel="api", external_user_id="u-accept")
                )
                await MessageRepository(sess).append(
                    app_id,
                    conv.id,
                    NewMessage(role="assistant", content="验收答案", answer_type="answer"),
                )
                sess.add(
                    ApiKey(
                        app_id=app_id,
                        name="验收 Key",
                        prefix=f"ak_{stamp}",
                        key_hash="h",
                        scopes=["chat"],
                        created_by=admin_id,
                    )
                )
                await sess.commit()
                conv_id = conv.id

            copied = await http.post(f"/api/v1/admin/apps/{app_id}/copy", headers=auth, json={})
            copied_body = copied.json()
            copy_id = UUID(copied_body["id"])
            created_apps.append(copy_id)
            t.check("复制 201", copied.status_code == 201, str(copied.status_code))
            t.check(
                "副本名称带（副本）", copied_body["name"].endswith("（副本）"), copied_body["name"]
            )
            t.check(
                "副本沿用配置（fuse_top_k=33）",
                copied_body["rag_config"]["fuse_top_k"] == 33,
                str(copied_body["rag_config"].get("fuse_top_k")),
            )
            async with sessions() as sess:
                copy_keys = (
                    await sess.execute(
                        select(func.count()).select_from(ApiKey).where(ApiKey.app_id == copy_id)
                    )
                ).scalar_one()
                copy_convs = (
                    await sess.execute(
                        select(func.count())
                        .select_from(Conversation)
                        .where(Conversation.app_id == copy_id)
                    )
                ).scalar_one()
                copy_docs = (
                    await sess.execute(
                        select(func.count()).select_from(Document).where(Document.app_id == copy_id)
                    )
                ).scalar_one()
            t.check(
                "副本没有 Key / 会话 / 文档",
                (copy_keys, copy_convs, copy_docs) == (0, 0, 0),
                f"{copy_keys}/{copy_convs}/{copy_docs}",
            )

            print("⑦ 删除应用并清理")
            removed = await http.delete(f"/api/v1/admin/apps/{app_id}", headers=auth)
            removed_body = removed.json()
            t.record("delete", status=removed.status_code, body=removed_body)
            t.check("删除 202", removed.status_code == 202, str(removed.status_code))
            t.check(
                "删除回 job_id / 吊销数 / 关闭会话数",
                set(removed_body) == {"job_id", "revoked_keys", "closed_conversations"}
                and removed_body["revoked_keys"] == 1
                and removed_body["closed_conversations"] == 1,
                str(removed_body),
            )
            gone = await http.get(f"/api/v1/admin/apps/{app_id}", headers=auth)
            gone_error = error_of(gone)
            t.check(
                "删除后详情 404",
                gone.status_code == 404 and gone_error.get("code") == "APP_NOT_FOUND",
                f"{gone.status_code}/{gone_error.get('code')}",
            )
            after_delete = await http.get("/api/v1/admin/apps", headers=auth)
            t.check(
                "删除后列表里消失",
                str(app_id) not in [a["id"] for a in after_delete.json()["items"]],
            )

            cleanup = await run_cleanup_app_job(app_id, UUID(removed_body["job_id"]))
            t.record("cleanup", status=cleanup["status"], deleted=cleanup["deleted"])
            t.check(
                "清理完成且删掉会话与消息",
                cleanup["status"] == "done"
                and cleanup["deleted"].get("conversations") == 1
                and cleanup["deleted"].get("messages") >= 1,
                str(cleanup["deleted"]),
            )
            async with sessions() as sess:
                rows = (
                    await sess.execute(
                        select(
                            select(func.count())
                            .select_from(ApiKey)
                            .where(ApiKey.app_id == app_id)
                            .scalar_subquery()
                        )
                    )
                ).scalar_one()
                conv_rows = (
                    await sess.execute(
                        select(
                            select(func.count())
                            .select_from(Conversation)
                            .where(Conversation.id == conv_id)
                            .scalar_subquery()
                        )
                    )
                ).scalar_one()
                app_rows = (
                    await sess.execute(
                        select(
                            select(func.count())
                            .select_from(App)
                            .where(App.id == app_id)
                            .scalar_subquery()
                        )
                    )
                ).scalar_one()
                job = (
                    await sess.execute(select(Job).where(Job.id == UUID(removed_body["job_id"])))
                ).scalar_one()
            t.check(
                "Key / 会话 / 应用行都已清空",
                (rows, conv_rows, app_rows) == (0, 0, 0),
                f"{rows}/{conv_rows}/{app_rows}",
            )
            t.check(
                "清理任务保留为平台级记录（app_id 空、done）",
                job.app_id is None and job.status == "done",
                f"{job.app_id}/{job.status}",
            )

            print("⑧ 清理任务重跑幂等")
            again = await run_cleanup_app_job(app_id, UUID(removed_body["job_id"]))
            t.check("重跑返回 already_done", again["status"] == "already_done", again["status"])

            print("⑨ 审计与收尾")
            logs = await http.get(
                "/api/v1/admin/audit", headers=auth, params={"page_size": 100, "action": ""}
            )
            actions = {item["action"] for item in logs.json()["items"]}
            t.check(
                "审计含 app.create / app.update / app.copy / app.delete / system.update",
                {"app.create", "app.update", "app.copy", "app.delete", "system.update"} <= actions,
                str(sorted(a for a in actions if a.startswith(("app.", "system.")))),
            )
            copy_removed = await http.delete(f"/api/v1/admin/apps/{copy_id}", headers=auth)
            t.check("删除副本 202", copy_removed.status_code == 202, str(copy_removed.status_code))
            await run_cleanup_app_job(copy_id, UUID(copy_removed.json()["job_id"]))
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=10)
        except (TimeoutError, asyncio.TimeoutError):  # noqa: UP041
            task.cancel()
        async with sessions() as sess:
            for aid in created_apps:
                await sess.execute(delete(Message).where(Message.app_id == aid))
                await sess.execute(delete(Conversation).where(Conversation.app_id == aid))
                await sess.execute(delete(ApiKey).where(ApiKey.app_id == aid))
                await sess.execute(delete(Document).where(Document.app_id == aid))
                await sess.execute(delete(UserAppRole).where(UserAppRole.app_id == aid))
                await sess.execute(delete(Job).where(Job.target_id == aid))
                await sess.execute(delete(App).where(App.id == aid))
            await sess.execute(delete(Job).where(Job.created_by == admin_id))
            # `system_settings.updated_by` 外键指向 users，先删设置行再删管理员
            await sess.execute(
                delete(SystemSetting).where(SystemSetting.key == "default_session_config")
            )
            await sess.execute(delete(User).where(User.email == admin_email))
            await sess.commit()
        await engine.dispose()

    return {
        "started_at": datetime.now(UTC).isoformat(),
        "checks": t.checks,
        "steps": t.steps,
        "passed": t.passed,
        "total": len(t.checks),
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # 本机控制台是 GBK
    result = asyncio.run(run())
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / "e2e_m42_apps.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{result['passed']}/{result['total']} 通过 → {out}")
    return 0 if result["passed"] == result["total"] else 1


if __name__ == "__main__":
    os.environ.setdefault("AICS_ENV", "dev")
    raise SystemExit(main())
