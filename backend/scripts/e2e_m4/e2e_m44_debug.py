"""M4.4 里程碑验收：调试对话（管理版 SSE + 检索 trace + 压缩）。

    # 本机 Docker 栈起好、迁移到 head、真 PG/Redis + 假模型（默认，0 元）
    PYTHONPATH=. uv run python -m scripts.e2e_m4.e2e_m44_debug

脚本在进程内起 uvicorn（`create_app()`），把 `app.state.chat_runtime` 换成
`tests/chat_support.make_runtime`（脚本模型 + 关键词知识库），因此不调用任何真实模型；
其余（PG / Redis / 鉴权 / 会话落库 / jobs）全部走真实实现。

判定项见末尾汇总：帧序、trace 内容、`channel=debug`、`retrieval_trace` 落库、临时参数
不写回应用、viewer 脱敏、压缩写 `jobs(type=compact)`、非法参数 422。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests.chat_support import make_runtime

from aics.core.config import get_settings
from aics.db.models import ApiKey, App, Conversation, Job, User, UserAppRole
from aics.main import create_app
from aics.modules.apps.service import AppService
from aics.modules.auth.service import AuthService

HERE = Path(__file__).resolve().parent
OUT = HERE / "results" / "e2e_m44_debug.json"
APP_NAME = "e2e-m44-debug"
PHONE = "13800138000"
MASKED = "138****8000"
ADMIN_EMAIL = "admin@aisc.com"
ADMIN_PASSWORD = "admin123456@"  # noqa: S105 — 本地开发默认口令，见 core/config.py


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _StubCompactor:
    """`compact_now` 的桩：固定返回一次成功压缩，验证接口与 job 落库。"""

    async def run(self, app_id: Any, conversation_id: Any, params: Any) -> Any:
        from aics.memory.compactor import CompactResult

        return CompactResult(
            result="success",
            version=1,
            batches=1,
            until_msg_id=uuid.uuid4(),
            facts=[],
            usage={"prompt": 10, "completion": 4},
        )


class Transcript:
    """每一步的响应体与判定；最后整份写 JSON，失败时也留下证据。"""

    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []
        self.checks: dict[str, bool] = {}

    def step(self, name: str, body: Any) -> None:
        self.steps.append({"step": name, "body": body})
        print(f"\n[{len(self.steps)}] {name}")
        print(f"    {json.dumps(body, ensure_ascii=False, default=str)[:700]}")

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks[name] = bool(ok)
        mark = "OK  " if ok else "FAIL"
        print(f"    {mark} {name}{'  → ' + detail if detail else ''}")

    @property
    def passed(self) -> int:
        return sum(1 for v in self.checks.values() if v)


async def prepare(sessions: Any) -> tuple[uuid.UUID, uuid.UUID, str, str]:
    """清掉上次的演示应用，建应用 + viewer 用户，返回 (app_id, viewer_id, viewer_email, 密码)."""
    password = f"Aics#{uuid.uuid4().hex[:10]}"
    async with sessions() as db:
        old = select(App.id).where(App.name == APP_NAME)
        await db.execute(delete(Conversation).where(Conversation.app_id.in_(old)))
        await db.execute(delete(ApiKey).where(ApiKey.app_id.in_(old)))
        await db.execute(delete(Job).where(Job.app_id.in_(old)))
        await db.execute(delete(App).where(App.name == APP_NAME))

        admin = (await db.execute(select(User).where(User.email == ADMIN_EMAIL))).scalar_one()
        app = await AppService(db).create_app(name=APP_NAME, created_by=admin.id)

        viewer_email = f"e2e-m44-viewer-{uuid.uuid4().hex[:8]}@x.io"
        auth = AuthService(db)
        viewer = await auth.create_user(
            email=viewer_email, display_name="只读成员", password=password
        )
        await auth.set_member(app.id, viewer.id, role_id="viewer", actor_id=admin.id)
        # 新建用户默认要求首登改密；验收脚本要直接用它读历史，先视为已完成改密。
        await db.execute(
            update(User).where(User.id == viewer.id).values(must_change_password=False)
        )
        await db.commit()
    return app.id, viewer.id, viewer_email, password


async def bootstrap_admin() -> None:
    proc = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "aics.cli", "bootstrap-admin"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"bootstrap-admin failed: {proc.stdout[-500:]}{proc.stderr[-800:]}")


async def login(client: httpx.AsyncClient, email: str, password: str) -> str:
    resp = await client.post(
        "/api/v1/admin/auth/login", json={"email": email, "password": password}
    )
    resp.raise_for_status()
    return str(resp.json()["access_token"])


def parse_sse(chunks: list[str]) -> list[tuple[str, Any]]:
    frames: list[tuple[str, Any]] = []
    event = ""
    data: list[str] = []
    for line in chunks:
        if not line:
            if event:
                raw = "\n".join(data)
                try:
                    frames.append((event, json.loads(raw) if raw else {}))
                except json.JSONDecodeError:
                    frames.append((event, raw))
                event, data = "", []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if event:
        raw = "\n".join(data)
        try:
            frames.append((event, json.loads(raw) if raw else {}))
        except json.JSONDecodeError:
            frames.append((event, raw))
    return frames


async def collect_turn(
    client: httpx.AsyncClient, headers: dict[str, str], app_id: uuid.UUID, body: dict[str, Any]
) -> list[tuple[str, Any]]:
    lines: list[str] = []
    async with client.stream(
        "POST", f"/api/v1/admin/apps/{app_id}/debug/chat", json=body, headers=headers, timeout=90
    ) as resp:
        if resp.status_code != 200:
            raw = (await resp.aread()).decode("utf-8", "replace")
            raise RuntimeError(f"debug chat {resp.status_code}: {raw[:400]}")
        async for line in resp.aiter_lines():
            lines.append(line)
    return parse_sse(lines)


def frame_of(frames: list[tuple[str, Any]], name: str) -> Any:
    for event, body in frames:
        if event == name:
            return body
    return None


async def run() -> dict[str, Any]:
    s = get_settings()
    t = Transcript()
    engine = create_async_engine(s.db_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    app_id, viewer_id, viewer_email, viewer_password = await prepare(sessions)
    await bootstrap_admin()

    application = create_app()
    runtime = make_runtime(sessions)
    # 验收集中在调试链路本身：压缩用一个固定结果的桩，避免为演示调模型。
    runtime.memory.compactor = _StubCompactor()
    application.state.chat_runtime = runtime

    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            application, host="127.0.0.1", port=port, access_log=False, log_level="warning"
        )
    )
    task = asyncio.create_task(server.serve())
    base = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient(base_url=base) as client:
            await _wait_ready(client)
            token = await login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
            headers = {"Authorization": f"Bearer {token}"}
            t.step("① 登录与演示应用", {"app_id": str(app_id), "viewer": viewer_email})

            before = (await client.get(f"/api/v1/admin/apps/{app_id}", headers=headers)).json()
            before_rag = before["rag_config"]
            t.check(
                "应用配置可读",
                isinstance(before_rag, dict),
                json.dumps(before_rag, ensure_ascii=False)[:60],
            )

            # ② 第一轮：完整帧序
            frames = await collect_turn(
                client, headers, app_id, {"question": "退货需要满足什么条件？"}
            )
            names = [name for name, _ in frames]
            start = frame_of(frames, "start") or {}
            end = frame_of(frames, "end") or {}
            trace = frame_of(frames, "trace") or {}
            citations = (frame_of(frames, "citations") or {}).get("citations", [])
            conversation_id = start.get("conversation_id")
            t.step(
                "② 第一轮调试对话（SSE）",
                {
                    "frames": names,
                    "conversation_id": conversation_id,
                    "answer": str(end.get("answer"))[:60],
                    "intent": (trace.get("intent") or {}).get("name"),
                    "citations": len(citations),
                },
            )
            t.check(
                "事件序 start → trace → citations → delta → end",
                names[:3] == ["start", "trace", "citations"]
                and names[-1] == "end"
                and "delta" in names,
                str(names[:6]),
            )
            t.check("end 携带非空 answer", bool(end.get("answer")), str(end.get("answer"))[:40])
            t.check(
                "trace 带决策与耗时",
                bool((trace.get("intent") or {}).get("name"))
                and isinstance(trace.get("timings"), dict),
                f"intent={(trace.get('intent') or {}).get('name')}",
            )
            t.check(
                "trace 带检索命中",
                len((trace.get("retrieval") or {}).get("vector") or []) >= 1,
                f"vector={(trace.get('retrieval') or {}).get('vector')}",
            )
            t.check("citations 非空", len(citations) >= 1, f"n={len(citations)}")

            # ③ 落库：channel=debug + retrieval_trace
            async with sessions() as db:
                conv = (
                    await db.execute(
                        select(Conversation).where(Conversation.id == uuid.UUID(conversation_id))
                    )
                ).scalar_one()
                from aics.db.models import Message

                rows = (
                    (
                        await db.execute(
                            select(Message)
                            .where(Message.conversation_id == conv.id)
                            .order_by(Message.created_at)
                        )
                    )
                    .scalars()
                    .all()
                )
            t.step(
                "③ 会话与消息落库",
                {"channel": conv.channel, "status": conv.status, "messages": len(rows)},
            )
            t.check("channel=debug", conv.channel == "debug", conv.channel)
            assistant = [r for r in rows if r.role == "assistant"]
            t.check(
                "assistant 消息带 retrieval_trace",
                bool(assistant)
                and isinstance(assistant[-1].retrieval_trace, dict)
                and "timings" in (assistant[-1].retrieval_trace or {}),
            )

            # ④ 临时参数：只影响本轮
            frames2 = await collect_turn(
                client,
                headers,
                app_id,
                {
                    "question": "退款多久到账？",
                    "conversation_id": conversation_id,
                    "debug_config": {"vector_top_k": 1},
                },
            )
            trace2 = frame_of(frames2, "trace") or {}
            start2 = frame_of(frames2, "start") or {}
            t.step(
                "④ 临时调试参数（vector_top_k=1）",
                {
                    "conversation_id": start2.get("conversation_id"),
                    "vector": (trace2.get("retrieval") or {}).get("vector"),
                },
            )
            t.check(
                "同会话续聊复用 conversation_id", start2.get("conversation_id") == conversation_id
            )
            t.check(
                "调试参数生效（向量命中被收窄到 1）",
                len((trace2.get("retrieval") or {}).get("vector") or []) == 1,
                f"vector={(trace2.get('retrieval') or {}).get('vector')}",
            )
            after = (await client.get(f"/api/v1/admin/apps/{app_id}", headers=headers)).json()
            t.check(
                "调试参数不写回应用配置",
                after["rag_config"] == before_rag,
                json.dumps(after["rag_config"], ensure_ascii=False)[:60],
            )

            # ⑤ 历史接口
            hist = (
                await client.get(
                    f"/api/v1/admin/apps/{app_id}/debug/conversations/{conversation_id}",
                    headers=headers,
                )
            ).json()
            t.step(
                "⑤ 历史消息",
                {"items": len(hist["items"]), "roles": [i["role"] for i in hist["items"]][:6]},
            )
            t.check(
                "历史 ≥ 4 条且首条是 user",
                len(hist["items"]) >= 4 and hist["items"][0]["role"] == "user",
            )
            t.check(
                "历史 assistant 带 trace",
                any(i.get("trace") for i in hist["items"] if i["role"] == "assistant"),
            )
            t.check(
                "历史回显正文未打码（超级管理员）", hist["items"][0]["content"].startswith("退货")
            )

            # ⑥ viewer 脱敏
            frames3 = await collect_turn(
                client,
                headers,
                app_id,
                {
                    "question": f"我的手机号是 {PHONE}，还能退货吗？",
                    "conversation_id": conversation_id,
                },
            )
            t.check("含手机号一轮仍有 end", bool(frame_of(frames3, "end")))
            viewer_token = await login(client, viewer_email, viewer_password)
            vresp = await client.get(
                f"/api/v1/admin/apps/{app_id}/debug/conversations/{conversation_id}",
                headers={"Authorization": f"Bearer {viewer_token}"},
            )
            vhist = vresp.json()
            if "items" not in vhist:
                raise RuntimeError(f"viewer history {vresp.status_code}: {vhist}")
            contents = " ".join(i["content"] for i in vhist["items"])
            t.step("⑥ viewer 读取历史", {"masked": MASKED in contents, "leaked": PHONE in contents})
            t.check("viewer 拿到打码后的正文", MASKED in contents and PHONE not in contents)
            t.check("viewer 有 chat.debug 权限（能读）", "code" not in vhist)

            # ⑦ 压缩：写 jobs(type=compact)
            compact = await client.post(
                f"/api/v1/admin/apps/{app_id}/debug/compact",
                json={"conversation_id": conversation_id},
                headers=headers,
            )
            body = compact.json()
            async with sessions() as db:
                job = (
                    await db.execute(select(Job).where(Job.id == uuid.UUID(body["job_id"])))
                ).scalar_one()
            t.step(
                "⑦ 手动压缩",
                {
                    "job_id": body.get("job_id"),
                    "compacted": body.get("compacted"),
                    "job.status": job.status,
                },
            )
            t.check(
                "压缩返回 job_id 且落库为 compact",
                job.type == "compact" and job.status in {"done", "running", "failed"},
                f"{job.type}/{job.status}",
            )
            t.check("压缩结果字段齐全", {"compacted", "batches", "version"} <= set(body))

            # ⑧ 非法调试参数：422 且不落新会话
            bad = await client.post(
                f"/api/v1/admin/apps/{app_id}/debug/chat",
                json={
                    "question": "随便聊聊",
                    "debug_config": {"vector_top_k": "many"},
                },
                headers=headers,
            )
            err = bad.json().get("error", {})
            locs = [e.get("loc") for e in err.get("details", {}).get("errors", [])]
            t.step("⑧ 非法调试参数", {"status": bad.status_code, "locs": locs[:2]})
            t.check("非法 debug_config → 422", bad.status_code == 422, str(bad.status_code))
            t.check(
                "错误 loc 指向 debug_config",
                any(
                    isinstance(loc, list) and loc[:2] == ["debug_config", "vector_top_k"]
                    for loc in locs
                ),
                str(locs[:2]),
            )
            async with sessions() as db:
                n = len(
                    (await db.execute(select(Conversation).where(Conversation.app_id == app_id)))
                    .scalars()
                    .all()
                )
            t.check("非法参数不新建会话", n == 1, f"conversations={n}")
    finally:
        server.should_exit = True
        await task
        await engine.dispose()
        await _cleanup(sessions)

    result = {
        "passed": t.passed,
        "total": len(t.checks),
        "checks": t.checks,
        "steps": t.steps,
        "mode": "stub-llm",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n{'=' * 60}\n通过 {t.passed}/{len(t.checks)} → {OUT}")
    return result


async def _wait_ready(client: httpx.AsyncClient) -> None:
    for _ in range(100):
        try:
            resp = await client.get("/readyz", timeout=2)
            if resp.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.2)
    raise RuntimeError("后端未在 20 秒内就绪")


async def _cleanup(sessions: Any) -> None:
    async with sessions() as db:
        old = select(App.id).where(App.name == APP_NAME)
        await db.execute(delete(Conversation).where(Conversation.app_id.in_(old)))
        await db.execute(delete(ApiKey).where(ApiKey.app_id.in_(old)))
        await db.execute(delete(Job).where(Job.app_id.in_(old)))
        await db.execute(delete(UserAppRole).where(UserAppRole.app_id.in_(old)))
        await db.execute(delete(App).where(App.name == APP_NAME))
        await db.execute(delete(User).where(User.email.like("e2e-m44-viewer-%")))
        await db.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    result = asyncio.run(run())
    raise SystemExit(0 if result["passed"] == result["total"] else 1)


if __name__ == "__main__":
    main()
