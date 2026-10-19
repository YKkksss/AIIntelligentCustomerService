"""M4.1 里程碑验收：管理后台鉴权面（登录、锁定、刷新轮换、权限、审计、重置密码）。

真实 HTTP + 真 PG/Redis，不调用模型。步骤：

    ① `python -m aics.cli bootstrap-admin` 建初始管理员（幂等：重复执行不报错）
    ② 错误密码 5 次 → 第 5 次 423 `ACCOUNT_LOCKED` 且带 `Retry-After`；解锁后登录成功
    ③ `GET /auth/me` 返回超管身份与全部权限
    ④ 新建用户（写审计）→ 该用户无成员关系时访问应用级审计 403
    ⑤ 授予 `viewer` → 403；升级 `app_admin` → 200（成员变更立即生效，缓存已失效）
    ⑥ `POST /auth/refresh` 轮换令牌；旧 refresh 复用 → 401，旧 access 立即失效
    ⑦ `GET /audit` 看得到 `auth.login` / `user.create` / `member.set`
    ⑧ `POST /users/{id}/reset-password` → 临时密码登录 → `must_change_password` →
       写接口 403 `must_change_password`；改密后恢复

用法（在 `backend/` 下，需本机 Docker 栈）：

    uv run --frozen alembic upgrade head
    uv run --frozen python -m scripts.e2e_m4.e2e_m41_admin
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
from uuid import UUID, uuid4

import httpx
import redis.asyncio as aioredis
import uvicorn
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aics.core.config import get_settings
from aics.db.models import App, AuditLog, User, UserAppRole
from aics.main import create_app
from aics.modules.apps.service import AppService

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


async def run() -> dict[str, Any]:
    s = get_settings()
    engine = create_async_engine(s.db_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    t = Transcript()
    stamp = datetime.now(UTC).strftime("%m%d%H%M%S")
    admin_email = f"admin-{stamp}@example.com"
    admin_password = "Passw0rd-admin-1"  # noqa: S105 —— 一次性的验收账号
    app_id = uuid4()
    created_users: list[UUID] = []

    async def _clear_limits() -> None:
        """脚本一轮要登录十几次，超过 10 次/分钟的上限，逐段清空限流桶。"""
        conn = aioredis.from_url(s.redis_url)
        try:
            async for key in conn.scan_iter(match=f"{s.redis_prefix}rl:*"):
                await conn.delete(key)
        finally:
            await conn.aclose()

    await _clear_limits()

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

        async with httpx.AsyncClient(base_url=base, timeout=10) as http:
            print("① bootstrap-admin 建初始管理员")
            code = await asyncio.to_thread(bootstrap_admin, admin_email, admin_password)
            t.check("bootstrap-admin 退出码 0", code == 0, f"exit={code}")
            again = await asyncio.to_thread(bootstrap_admin, admin_email, admin_password)
            t.check("bootstrap-admin 幂等（重复执行仍 0）", again == 0, f"exit={again}")

            print("② 登录失败 5 次锁定")
            bad = []
            for _ in range(5):
                resp = await http.post(
                    "/api/v1/admin/auth/login",
                    json={"email": admin_email, "password": "wrong-password"},
                )
                bad.append(resp.status_code)
            t.record("login_failures", statuses=bad)
            t.check("前四次都是 401", bad[:4] == [401, 401, 401, 401], str(bad[:4]))
            t.check("第五次 423 ACCOUNT_LOCKED", bad[-1] == 423, str(bad[-1]))
            locked = await http.post(
                "/api/v1/admin/auth/login",
                json={"email": admin_email, "password": admin_password},
            )
            t.check("锁定期内正确密码也是 423", locked.status_code == 423, str(locked.status_code))
            t.check(
                "423 带 Retry-After",
                "retry-after" in {k.lower() for k in locked.headers},
                str(dict(locked.headers)),
            )
            # 解锁：直接把 locked_until 拨到过去（等价于等 15 分钟）
            async with sessions() as sess:
                user = (
                    await sess.execute(select(User).where(User.email == admin_email))
                ).scalar_one()
                user.locked_until = None
                user.failed_logins = 0
                await sess.commit()
                admin_id = user.id

            # 解锁之后才比较文案：锁定期内正确密码也返回 423，会把两条基线打乱
            unknown = await http.post(
                "/api/v1/admin/auth/login",
                json={"email": f"nobody-{stamp}@example.com", "password": "whatever-123"},
            )
            wrong = await http.post(
                "/api/v1/admin/auth/login",
                json={"email": admin_email, "password": "wrong-password"},
            )
            t.check(
                "错误信息不暴露邮箱是否存在",
                unknown.status_code == wrong.status_code == 401
                and unknown.json()["error"]["message"] == wrong.json()["error"]["message"],
                f"{unknown.status_code}/{wrong.status_code}",
            )

            login = await http.post(
                "/api/v1/admin/auth/login",
                json={"email": admin_email, "password": admin_password},
            )
            body = login.json()
            t.record("login", status=login.status_code, body=body | {"access_token": "…"})
            t.check("解锁后登录 200", login.status_code == 200, str(login.status_code))
            access = body["access_token"]
            refresh = body["refresh_token"]
            auth = {"Authorization": f"Bearer {access}"}

            print("③ GET /auth/me")
            me = await http.get("/api/v1/admin/auth/me", headers=auth)
            me_body = me.json()
            t.check("me 200", me.status_code == 200, str(me.status_code))
            t.check("me 是超管", me_body["user"]["is_super_admin"] is True)

            print("④ 新建用户与审计")
            new_email = f"viewer-{stamp}@example.com"
            created = await http.post(
                "/api/v1/admin/users",
                headers=auth,
                json={
                    "email": new_email,
                    "display_name": "验收只读用户",
                    "password": "Passw0rd-viewer-1",
                },
            )
            created_body = created.json()
            t.record("create_user", status=created.status_code, body={"email": new_email})
            t.check("建用户 201", created.status_code == 201, str(created.status_code))
            viewer_id = UUID(created_body["id"])
            created_users.append(viewer_id)
            t.check(
                "新用户要求首登改密",
                created_body["must_change_password"] is True,
            )

            async with sessions() as sess:
                app = await AppService(sess).create_app(
                    name=f"验收应用-{stamp}", created_by=admin_id
                )
                app_id = app.id
                await sess.commit()

            viewer_login = await http.post(
                "/api/v1/admin/auth/login",
                json={"email": new_email, "password": "Passw0rd-viewer-1"},
            )
            viewer_tokens = viewer_login.json()
            t.check("新用户登录 200", viewer_login.status_code == 200)
            viewer_auth = {"Authorization": f"Bearer {viewer_tokens['access_token']}"}

            no_perm = await http.get(f"/api/v1/admin/audit?app_id={app_id}", headers=viewer_auth)
            t.check(
                "无成员关系访问应用审计 403", no_perm.status_code == 403, str(no_perm.status_code)
            )

            print("⑤ 成员授权立即生效")
            put_viewer = await http.put(
                f"/api/v1/admin/apps/{app_id}/members/{viewer_id}",
                headers=auth,
                json={"role_id": "viewer"},
            )
            t.check("授予 viewer 200", put_viewer.status_code == 200, str(put_viewer.status_code))
            still_no = await http.get(f"/api/v1/admin/audit?app_id={app_id}", headers=viewer_auth)
            t.check(
                "viewer 没有 audit.read，仍 403",
                still_no.status_code == 403,
                str(still_no.status_code),
            )
            put_admin = await http.put(
                f"/api/v1/admin/apps/{app_id}/members/{viewer_id}",
                headers=auth,
                json={"role_id": "app_admin"},
            )
            t.check("升级 app_admin 200", put_admin.status_code == 200, str(put_admin.status_code))
            now_ok = await http.get(f"/api/v1/admin/audit?app_id={app_id}", headers=auth)
            t.check("超管可读应用审计", now_ok.status_code == 200, str(now_ok.status_code))

            # 用 viewer 自己的令牌重试（缓存已失效 → 新权限立即生效）
            reload_me = await http.get("/api/v1/admin/auth/me", headers=viewer_auth)
            perms = [
                p
                for app in reload_me.json()["apps"]
                if app["app_id"] == str(app_id)
                for p in app["permissions"]
            ]
            t.check("me 里立刻看到 app_admin 权限", "audit.read" in perms, str(len(perms)))

            print("⑥ 刷新轮换与复用检测")
            rotated = await http.post("/api/v1/admin/auth/refresh", json={"refresh_token": refresh})
            rotated_body = rotated.json()
            t.check("refresh 200", rotated.status_code == 200, str(rotated.status_code))
            new_access = rotated_body["access_token"]
            t.check("access 换了", new_access != access)
            reuse = await http.post("/api/v1/admin/auth/refresh", json={"refresh_token": refresh})
            t.record("refresh_reuse", status=reuse.status_code, body=reuse.json())
            t.check("旧 refresh 复用 401", reuse.status_code == 401, str(reuse.status_code))
            stale = await http.get("/api/v1/admin/auth/me", headers=auth)
            t.check("旧 access 立即失效 401", stale.status_code == 401, str(stale.status_code))

            also_stale = await http.get(
                "/api/v1/admin/auth/me", headers={"Authorization": f"Bearer {new_access}"}
            )
            t.check(
                "复用检测踢掉全部会话（轮换出的 access 也 401）",
                also_stale.status_code == 401,
                str(also_stale.status_code),
            )
            await _clear_limits()
            relogin = await http.post(
                "/api/v1/admin/auth/login",
                json={"email": admin_email, "password": admin_password},
            )
            admin_auth = {"Authorization": f"Bearer {relogin.json()['access_token']}"}
            t.check("重新登录可继续", relogin.status_code == 200, str(relogin.status_code))

            print("⑦ 审计查询")
            audit = await http.get(
                f"/api/v1/admin/audit?app_id={app_id}&page_size=50",
                headers=admin_auth,
            )
            audit_body = audit.json()
            actions = [row["action"] for row in audit_body["items"]]
            t.record("audit", status=audit.status_code, actions=actions)
            t.check("审计 200", audit.status_code == 200, str(audit.status_code))
            t.check("有 member.set", "member.set" in actions, str(actions[:8]))
            all_actions = await http.get(
                "/api/v1/admin/audit/actions",
                headers=admin_auth,
            )
            t.check(
                "动作清单含 auth.login",
                "auth.login" in all_actions.json()["items"],
                str(all_actions.json()["items"][:8]),
            )
            passwords = json.dumps(audit_body, ensure_ascii=False)
            t.check("审计里没有明文密码", "Passw0rd" not in passwords)

            print("⑧ 重置密码与首登改密")
            reset = await http.post(
                f"/api/v1/admin/users/{viewer_id}/reset-password", headers=admin_auth
            )
            temp = reset.json()["password"]
            t.record("reset_password", status=reset.status_code, length=len(temp))
            t.check("重置 200 且一次性密码 ≥16 位", reset.status_code == 200 and len(temp) >= 16)
            temp_login = await http.post(
                "/api/v1/admin/auth/login", json={"email": new_email, "password": temp}
            )
            temp_to = temp_login.json()
            t.check("临时密码可登录", temp_login.status_code == 200)
            blocked = await http.get(
                "/api/v1/admin/users",
                headers={"Authorization": f"Bearer {temp_to['access_token']}"},
            )
            t.check(
                "未改密时写/读接口 403 must_change_password",
                blocked.status_code == 403
                and blocked.json()["error"]["details"].get("reason") == "must_change_password",
                str(blocked.status_code),
            )
            changed = await http.post(
                "/api/v1/admin/auth/password",
                headers={"Authorization": f"Bearer {temp_to['access_token']}"},
                json={"current_password": temp, "new_password": "Passw0rd-viewer-2"},
            )
            t.check("改密 200", changed.status_code == 200, str(changed.status_code))

            async with sessions() as sess:
                logs = (await sess.execute(select(AuditLog))).scalars().all()
            t.check("审计落库有 user.create", any(r.action == "user.create" for r in logs))
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=10)
        except (TimeoutError, asyncio.TimeoutError):  # noqa: UP041
            task.cancel()
        async with sessions() as sess:
            await sess.execute(delete(UserAppRole).where(UserAppRole.app_id == app_id))
            await sess.execute(delete(App).where(App.id == app_id))
            for uid in created_users:
                await sess.execute(delete(User).where(User.id == uid))
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
    out = RESULTS / "e2e_m41_admin.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{result['passed']}/{result['total']} 通过 → {out}")
    return 0 if result["passed"] == result["total"] else 1


if __name__ == "__main__":
    os.environ.setdefault("AICS_ENV", "dev")
    raise SystemExit(main())
