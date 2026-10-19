"""M3.4 里程碑验收：脚本模拟坐席平台跑通转人工全链路（真 HTTP / 真 PG / 真 Redis）。

    cd backend
    PYTHONPATH=. uv run python -m scripts.e2e_m3.e2e_m34_handoff --stub-llm   # 假模型，0 元
    PYTHONPATH=. uv run python -m scripts.e2e_m3.e2e_m34_handoff --real-llm   # 真模型，计划 < 0.1 元

`aics.main.create_app()` 用 `.env` 里的本机 PG / Redis 起在随机端口上，坐席平台侧只走文档化的
开放接口（`04-api/02-开放接口参考.md` §5），不 import 服务层：

    ① 用户说「我要人工」→ 转人工排队（`handoff_status=pending`，答复由系统给出）
    ② 坐席平台 `GET /handoffs?status=pending` 看到这条会话
    ③ `POST …/handoff/accept` 抢单 → `accepted`；第二个坐席再抢 → `409 HANDOFF_STATE_CONFLICT`
    ④ 坐席发言 → 201
    ⑤ 用户 `GET …/messages` 看到 `role=human_agent` 的消息
    ⑥ 坐席 `release` → `released`
    ⑦ 用户接着问 → AI 恢复应答（`reply_by=ai`）
    ⑧ 排队超时：新会话排队后把时钟推过 `handoff_queue_timeout_minutes` 再扫描 → 自动释放、
       置待回访、发排队超时话术（扫描是 worker 里的同一个 `sweep`，这里在进程内直接调用）
    ⑨ 非服务时间：服务时间置空后用户再转人工 → 留言（`reply_by=system`）+ 待回访，不排队

`--stub-llm`（默认）把 `app.state.chat_runtime` 换成测试用的脚本模型 + 关键词知识库
（`tests/chat_support.make_runtime`），因此不调用任何真实模型；`--real-llm` 走生产装配
（真 DeepSeek + 百炼 + Qdrant），用于计划里的 6 轮真实对话演示。

每一步的响应体与结论写进 `scripts/e2e_m3/results/e2e_m34_handoff.json`（失败也写，便于排查）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import uvicorn
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aics.core.config import get_settings
from aics.db.models import ApiKey, App, Conversation, Message, User
from aics.main import create_app
from aics.modules.apikeys.service import ApiKeyService
from aics.modules.apps.service import AppService
from aics.modules.handoff import sweep as sweep_mod

HERE = Path(__file__).resolve().parent
OUT = HERE / "results" / "e2e_m34_handoff.json"
APP_NAME = "e2e-m34-handoff"
#: `handoff.keywords` 默认含「人工」，一句话就命中（`rag/config.py` 的 `HandoffConfig`）
ASK_HUMAN = "我要人工"
#: 服务时间外的用户问法：`release` 之后 AI 恢复，这一问走 RAG（假模型按关键词作答）
ASK_KB = "退货需要满足什么条件？"
#: 排队期间的第二问，用来验证 wait 模式（系统回 `wait_text`、不调模型）
WAIT_QUESTION = "那大概要等多久？"
WAIT_TEXT = "已为您转接人工客服，请稍候，坐席忙完会立刻回复您。"
AGENT_ID, AGENT_NAME, AGENT2_ID = "ag_demo_1", "小柯", "ag_demo_2"
QUEUE_TIMEOUT_MIN = 1
M = timedelta(minutes=1)
#: 每周每天都没有服务时段 = 7×24 之外（`in_service_hours` 对空时段返回 False）
NO_HOURS = {
    "timezone": "UTC",
    "weekly": {d: [] for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")},
}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Transcript:
    """每步的响应体与判定；最后整份写 JSON，失败时也留下证据。"""

    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []
        self.checks: dict[str, bool] = {}

    def step(self, name: str, body: Any) -> None:
        self.steps.append({"step": name, "body": body})
        print(f"\n[{len(self.steps)}] {name}")
        print(f"    {json.dumps(body, ensure_ascii=False, default=str)[:600]}")

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks[name] = bool(ok)
        mark = "OK  " if ok else "FAIL"
        print(f"    {mark} {name}{'  ← ' + detail if detail else ''}")

    @property
    def passed(self) -> int:
        return sum(1 for v in self.checks.values() if v)


async def prepare(sessions: Any, s: Any) -> tuple[UUID, str]:
    """清掉上次运行的演示数据，建一个演示应用 + 一把 `chat` + `handoff:write` 的 Key。"""
    async with sessions() as db:
        old = select(App.id).where(App.name == APP_NAME)
        await db.execute(delete(Conversation).where(Conversation.app_id.in_(old)))
        await db.execute(delete(ApiKey).where(ApiKey.app_id.in_(old)))
        await db.execute(delete(App).where(App.name == APP_NAME))
        # password_hash 只是占位（本脚本不登录），S106 在这里没有意义
        u = User(
            email=f"e2e-m34-{uuid.uuid4().hex[:8]}@x.io",
            display_name="e2e",
            password_hash="h",  # noqa: S106
        )
        db.add(u)
        await db.flush()
        app = await AppService(db).create_app(
            name=APP_NAME,
            created_by=u.id,
            # 演示用「排队等坐席」：`wait` 模式下一轮起由系统回排队话术（`continue` 是默认，
            # 那种模式下排队期间 AI 仍照常回答）
            handoff_config={
                "handoff_queue_timeout_minutes": QUEUE_TIMEOUT_MIN,
                "handoff_ai_mode": "wait",
                "wait_text": WAIT_TEXT,
            },
        )
        await db.flush()
        app_id = app.id
        _, plain = await ApiKeyService(db, pepper=s.api_key_pepper.get_secret_value()).create(
            app_id, name="e2e-m34", scopes=["chat", "handoff:write"], rpm_limit=1000
        )
        await db.commit()
    return app_id, plain


async def patch_app(sessions: Any, app_id: UUID, patch: dict[str, Any]) -> None:
    async with sessions() as db:
        await AppService(db).update_app(app_id, patch)
        await db.commit()


async def messages_of(sessions: Any, app_id: UUID, cid: UUID) -> list[dict[str, Any]]:
    async with sessions() as db:
        rows = await db.execute(
            select(Message)
            .where(Message.app_id == app_id, Message.conversation_id == cid)
            .order_by(Message.created_at)
        )
        return [
            {"role": m.role, "kind": m.kind, "content": m.content, "answer_type": m.answer_type}
            for m in rows.scalars()
        ]


async def conversation_of(sessions: Any, app_id: UUID, cid: UUID) -> dict[str, Any]:
    async with sessions() as db:
        c = (
            await db.execute(
                select(Conversation).where(Conversation.app_id == app_id, Conversation.id == cid)
            )
        ).scalar_one()
        return {
            "status": c.status,
            "close_reason": c.close_reason,
            "handoff_status": c.handoff_status,
            "handoff_end_reason": c.handoff_end_reason,
            "needs_callback": c.needs_callback,
            "callback_reason": c.callback_reason,
            "handoff_agent_id": c.handoff_agent_id,
        }


async def run(*, real_llm: bool) -> dict[str, Any]:
    s = get_settings()
    engine = create_async_engine(s.db_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tx = Transcript()
    app_id, key = await prepare(sessions, s)
    print(f"演示应用 {app_id}（{APP_NAME}），Key 前缀 {key[:12]}…")

    app = create_app()
    if not real_llm:
        # 假模型：复用测试里的脚本模型 + 关键词知识库，本机没有模型 Key 也能跑完整条链路。
        from tests.chat_support import make_runtime

        app.state.chat_runtime = make_runtime(sessions)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, port=port, access_log=False, log_level="warning"))
    serve = asyncio.create_task(server.serve())
    base = f"http://127.0.0.1:{port}"
    head = f"{base}/api/v1/apps/{app_id}"
    headers = {"Authorization": f"Bearer {key}"}
    started = datetime.now(UTC)
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            for _ in range(150):
                if serve.done():
                    raise RuntimeError("uvicorn 提前退出")
                try:
                    if (await c.get(f"{base}/healthz")).status_code < 500:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.2)

            # ① 用户说要人工 → 排队
            r = await c.post(f"{head}/chat", json={"question": ASK_HUMAN}, headers=headers)
            turn = r.json()
            cid = UUID(turn["conversation_id"])
            tx.step(
                "① 用户「我要人工」→ 转人工排队",
                {
                    "status": r.status_code,
                    "handoff": turn.get("handoff"),
                    "handoff_status": turn.get("handoff_status"),
                    "handoff_reason": turn.get("handoff_reason"),
                    "reply_by": turn.get("reply_by"),
                    "answer": turn.get("answer"),
                },
            )
            tx.check(
                "HTTP 200 且 handoff=true", r.status_code == 200 and turn.get("handoff") is True
            )
            tx.check("状态 pending", turn.get("handoff_status") == "pending")
            tx.check(
                "转人工那一轮仍由 AI 回话术",
                turn.get("reply_by") == "ai",
                str(turn.get("reply_by")),
            )

            # ①b 排队期间的下一轮：wait 模式下 `dispatch_of(pending, wait)` → 系统回 `wait_text`，
            # 不再调模型（这也是 `handoff_ai_mode` 两种取值的区别所在）
            r = await c.post(
                f"{head}/chat",
                json={"question": WAIT_QUESTION, "conversation_id": str(cid)},
                headers=headers,
            )
            waiting = r.json()
            tx.step(
                "①b 排队期间再问一句（wait 模式）",
                {
                    "status": r.status_code,
                    "reply_by": waiting.get("reply_by"),
                    "answer": waiting.get("answer"),
                    "handoff_status": waiting.get("handoff_status"),
                },
            )
            tx.check(
                "排队答由系统给出",
                waiting.get("reply_by") == "system",
                str(waiting.get("reply_by")),
            )
            tx.check(
                "排队话术就是配置的 wait_text",
                waiting.get("answer") == WAIT_TEXT,
                str(waiting.get("answer")),
            )

            # ② 坐席平台拉待接入列表
            r = await c.get(f"{head}/handoffs", params={"status": "pending"}, headers=headers)
            board = r.json()
            first = board["items"][0] if board["items"] else {}
            tx.step(
                "② 坐席平台 GET /handoffs?status=pending",
                {"status": r.status_code, "items": board["items"][:3]},
            )
            tx.check("列表里能看到这条会话", any(i["id"] == str(cid) for i in board["items"]))
            tx.check(
                "列表项字段齐全（id/状态/请求时间/待回访/消息数）",
                {"id", "handoff_status", "requested_at", "needs_callback", "message_count"}
                <= set(first),
            )

            # ③ 抢单接入
            r = await c.post(
                f"{head}/conversations/{cid}/handoff/accept",
                json={"agent_id": AGENT_ID, "agent_name": AGENT_NAME},
                headers=headers,
            )
            acc = r.json()
            tx.step("③ 坐席 accept 抢单", acc)
            tx.check("状态 accepted", acc.get("handoff_status") == "accepted")
            tx.check(
                "记下坐席与接入时间",
                acc.get("agent_id") == AGENT_ID
                and acc.get("agent_name") == AGENT_NAME
                and acc.get("accepted_at"),
            )

            r = await c.post(
                f"{head}/conversations/{cid}/handoff/accept",
                json={"agent_id": AGENT2_ID},
                headers=headers,
            )
            tx.step(
                "③b 第二个坐席再抢（并发抢单的失败侧）", {"status": r.status_code, "body": r.json()}
            )
            tx.check(
                "409 HANDOFF_STATE_CONFLICT",
                r.status_code == 409 and "HANDOFF_STATE_CONFLICT" in r.text,
            )

            # ④ 坐席发言
            r = await c.post(
                f"{head}/conversations/{cid}/handoff/messages",
                json={"agent_id": AGENT_ID, "content": "您好，我是人工客服小柯，请把订单号发我。"},
                headers=headers,
            )
            tx.step("④ 坐席发言", {"status": r.status_code, "message": r.json()})
            tx.check(
                "201 且 role=human_agent",
                r.status_code == 201 and r.json().get("role") == "human_agent",
            )

            # ⑤ 用户侧消息列表
            r = await c.get(f"{head}/conversations/{cid}/messages", headers=headers)
            items = r.json()["items"]
            tx.step("⑤ 用户 GET …/messages", {"status": r.status_code, "items": items})
            tx.check("用户能看到人工消息", any(m["role"] == "human_agent" for m in items))
            tx.check("人工消息在用户消息之后", [m["role"] for m in items][-1] == "human_agent")

            # ⑥ 交还 AI
            r = await c.post(
                f"{head}/conversations/{cid}/handoff/release",
                json={"agent_id": AGENT_ID, "note": "已答复"},
                headers=headers,
            )
            rel = r.json()
            tx.step("⑥ 坐席 release 交还 AI", rel)
            tx.check("状态 released", rel.get("handoff_status") == "released")

            # ⑦ AI 恢复应答
            r = await c.post(
                f"{head}/chat",
                json={"question": ASK_KB, "conversation_id": str(cid)},
                headers=headers,
            )
            back = r.json()
            tx.step(
                "⑦ 用户接着问 → AI 恢复应答",
                {
                    "status": r.status_code,
                    "reply_by": back.get("reply_by"),
                    "answer": back.get("answer"),
                },
            )
            tx.check("回复方是 AI", back.get("reply_by") == "ai", str(back.get("reply_by")))
            # `handoff_status` 保留最后一次的值（`released`），这里要看的是本轮没有**再次**触发转人工
            tx.check("本轮没有再次转人工", back.get("handoff") is False, str(back.get("handoff")))

            # ⑧ 排队超时（worker 里的同一个 sweep，进程内直接调用）
            r = await c.post(f"{head}/chat", json={"question": ASK_HUMAN}, headers=headers)
            cid2 = UUID(r.json()["conversation_id"])
            await sweep_mod.sweep(sessions, now=datetime.now(UTC) + (QUEUE_TIMEOUT_MIN + 1) * M)
            state2 = (await c.get(f"{head}/conversations/{cid2}/handoff", headers=headers)).json()
            msgs2 = await messages_of(sessions, app_id, cid2)
            tx.step(
                f"⑧ 排队超时：时钟推过 {QUEUE_TIMEOUT_MIN + 1} 分钟后再扫描",
                {"handoff": state2, "messages": msgs2},
            )
            tx.check("自动释放", state2.get("handoff_status") == "released")
            tx.check(
                "交还原因是 queue_timeout", state2.get("handoff_end_reason") == "queue_timeout"
            )
            tx.check("置待回访（坐席没接上）", state2.get("needs_callback") is True)
            tx.check("留下排队超时话术", any(m["role"] == "system" for m in msgs2))

            # ⑨ 非服务时间
            await patch_app(sessions, app_id, {"handoff_config": {"service_hours": NO_HOURS}})
            r = await c.post(f"{head}/chat", json={"question": ASK_HUMAN}, headers=headers)
            off = r.json()
            cid3 = UUID(off["conversation_id"])
            state3 = (await c.get(f"{head}/conversations/{cid3}/handoff", headers=headers)).json()
            msgs3 = await messages_of(sessions, app_id, cid3)
            tx.step(
                "⑨ 非服务时间：用户再转人工",
                {
                    "turn": {
                        k: off.get(k)
                        for k in ("handoff_status", "handoff_reason", "reply_by", "answer")
                    },
                    "handoff": state3,
                    "messages": msgs3,
                },
            )
            tx.check(
                "不排队，直接释放",
                off.get("handoff_status") == "released"
                and state3.get("handoff_end_reason") == "offline",
            )
            tx.check(
                "回留言话术（系统回复）",
                off.get("reply_by") == "system" and bool(off.get("answer")),
            )
            tx.check("置待回访", state3.get("needs_callback") is True)
            tx.check(
                "落库 close_reason 不变（会话仍 active）",
                (await conversation_of(sessions, app_id, cid3))["status"] == "active",
            )

        server.should_exit = True
        await serve
    finally:
        await engine.dispose()

    result = {
        "app": APP_NAME,
        "app_id": str(app_id),
        "key_prefix": key[:12],
        "mode": "real-llm" if real_llm else "stub-llm",
        "stub_llm": not real_llm,
        "started_at": started.isoformat(),
        "seconds": round((datetime.now(UTC) - started).total_seconds(), 1),
        "checks": tx.checks,
        "passed": f"{tx.passed}/{len(tx.checks)}",
        "steps": tx.steps,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n验收结论：{tx.passed}/{len(tx.checks)} 项通过；证据写入 {OUT}")
    return result


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        # Windows 控制台默认 GBK：生僻字不该把整轮演示打断（证据文件始终是 UTF-8）
        sys.stdout.reconfigure(errors="replace")
    p = argparse.ArgumentParser(description="M3.4 转人工全链路验收（真 HTTP + 真 PG + 真 Redis）")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--stub-llm", action="store_true", help="用测试脚本模型（默认，0 元）")
    g.add_argument("--real-llm", action="store_true", help="走生产装配，真 DeepSeek + 百炼")
    args = p.parse_args()
    asyncio.run(run(real_llm=args.real_llm))


if __name__ == "__main__":
    main()
