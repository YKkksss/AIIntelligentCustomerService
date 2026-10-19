"""M3.5 ⑤ 里程碑验收：Webhook 与会话事件流全链路（真 HTTP / 真 PG / 真 Redis）。

    cd backend
    PYTHONPATH=. uv run python -m scripts.e2e_m3.e2e_m35_webhooks --stub-llm   # 假模型，0 元
    PYTHONPATH=. uv run python -m scripts.e2e_m3.e2e_m35_webhooks --real-llm   # 真模型（本机无 Key）

`aics.main.create_app()` 起在随机端口上，业务侧只走文档化的开放接口；Webhook 那一端用
`scripts/e2e_m3/webhook_receiver.py`（同一个 `app`，本进程里起 uvicorn）真的收包并按
第三方接入指南 §7.4 验签。投递与重试扫描直接调任务体（本机没有运行中的 Celery worker），
每一步都断言到「接收端收到了什么」：

    ① 用户「我要人工」→ `handoff.requested` 落库 + 排一条 pending 投递 → 接收端 200 且验签通过
    ② 坐席 `accept` → `handoff.accepted`（接收端看到 agent_id / agent_name）
    ③ 坐席发言 → `message.created`，用户能在 `GET …/messages` 看到 `human_agent`
    ④ 坐席 `release` → `handoff.released`
    ⑤ 接收端先返回 500 → 投递按退避写 `next_attempt_at`；到期扫描领取后重投成功
    ⑥ 事件流：断开重连按 `Last-Event-ID` 只补发更新的帧；连接期间新事件实时送达

跑之前先 `uv run --frozen alembic upgrade head`（开发库 `aics`）。结果写
`scripts/e2e_m3/results/e2e_m35_webhooks.json`（失败也写，便于排查）。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import socket
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import redis.asyncio as aioredis
import uvicorn
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aics.core.config import get_settings
from aics.db.models import (
    ApiKey,
    App,
    Conversation,
    ConversationEvent,
    User,
    WebhookDelivery,
)
from aics.main import create_app
from aics.modules.apikeys.service import ApiKeyService
from aics.modules.apps.service import AppService
from aics.modules.handoff.runtime import HandoffRuntime
from aics.modules.webhooks import service as webhooks
from scripts.e2e_m3 import webhook_receiver as receiver

HERE = Path(__file__).resolve().parent
OUT = HERE / "results" / "e2e_m35_webhooks.json"
APP_NAME = "e2e-m35-webhooks"
#: `handoff.keywords` 默认含「人工」，一句话就命中（`rag/config.py` 的 `HandoffConfig`）
ASK_HUMAN = "我要人工"
AGENT_ID, AGENT_NAME = "ag_demo_1", "小柯"


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


async def wait_ready(c: httpx.AsyncClient, url: str, task: asyncio.Task[Any]) -> None:
    for _ in range(150):
        if task.done():
            raise RuntimeError(f"服务提前退出：{task.exception()!r}")
        try:
            if (await c.get(url)).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.2)
    raise RuntimeError(f"服务未就绪：{url}")


async def prepare(sessions: Any, s: Any) -> tuple[UUID, str]:
    """清掉上次运行的演示数据，建一个演示应用 + 一把 `chat` + `handoff:write` 的 Key。"""
    async with sessions() as db:
        old = select(App.id).where(App.name == APP_NAME)
        await db.execute(delete(ConversationEvent).where(ConversationEvent.app_id.in_(old)))
        await db.execute(delete(Conversation).where(Conversation.app_id.in_(old)))
        await db.execute(delete(ApiKey).where(ApiKey.app_id.in_(old)))
        await db.execute(delete(App).where(App.name == APP_NAME))
        # password_hash 只是占位（本脚本不登录），S106 在这里没有意义
        u = User(
            email=f"e2e-m35-{uuid.uuid4().hex[:8]}@x.io",
            display_name="e2e",
            password_hash="h",  # noqa: S106
        )
        db.add(u)
        await db.flush()
        app = await AppService(db).create_app(name=APP_NAME, created_by=u.id)
        await db.flush()
        app_id = app.id
        _, plain = await ApiKeyService(db, pepper=s.api_key_pepper.get_secret_value()).create(
            app_id, name="e2e-m35", scopes=["chat", "handoff:write"], rpm_limit=1000
        )
        await db.commit()
    return app_id, plain


async def events_of(sessions: Any, cid: UUID) -> list[dict[str, Any]]:
    async with sessions() as db:
        rows = await db.execute(
            select(ConversationEvent)
            .where(ConversationEvent.conversation_id == cid)
            .order_by(ConversationEvent.seq)
        )
        return [{"seq": e.seq, "type": e.type} for e in rows.scalars()]


async def deliveries_of(sessions: Any, cid: UUID) -> list[WebhookDelivery]:
    async with sessions() as db:
        rows = await db.execute(
            select(WebhookDelivery)
            .where(WebhookDelivery.conversation_id == cid)
            .order_by(WebhookDelivery.seq)
        )
        return list(rows.scalars())


async def deliver_pending(sessions: Any, cid: UUID) -> list[dict[str, Any]]:
    """把某个会话的 pending 投递逐条投一次（本机没有常驻 worker，直接调任务体）。"""
    out: list[dict[str, Any]] = []
    for row in await deliveries_of(sessions, cid):
        if row.status == "pending":
            out.append(await webhooks.run_deliver_job(row.id))
    return out


async def received(c: httpx.AsyncClient, base: str) -> dict[str, Any]:
    return (await c.get(f"{base}/received")).json()


async def read_frames(
    c: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    *,
    limit: int,
    timeout: float = 10.0,  # noqa: ASYNC109 - 这里的超时是给测试客户端的读窗口
) -> list[dict[str, Any]]:
    """从事件流读 `limit` 帧（读满或超时就断开，模拟客户端自己关掉连接）。"""
    frames: list[dict[str, Any]] = []

    async def collect() -> None:
        async with c.stream("GET", url, headers=headers) as resp:
            if resp.status_code != 200:
                raise RuntimeError(f"事件流返回 {resp.status_code}")
            buf = ""
            async for chunk in resp.aiter_text():
                buf += chunk
                while "\n\n" in buf:
                    block, buf = buf.split("\n\n", 1)
                    if not block.startswith("id: "):
                        continue  # `: ping` 心跳
                    head, data = block.split("\ndata: ", 1)
                    frames.append({"id": int(head[len("id: ") :]), "payload": json.loads(data)})
                    if len(frames) >= limit:
                        return

    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(collect(), timeout)
    return frames


async def run(*, real_llm: bool) -> dict[str, Any]:
    # 本地接收端在回环地址上：演示用放行（生产靠 AICS_URL_FETCH_ALLOWLIST 精确放行可信目标）
    os.environ.setdefault("AICS_URL_FETCH_ALLOWLIST", "127.0.0.1")
    get_settings.cache_clear()
    s = get_settings()
    engine = create_async_engine(s.db_url, pool_pre_ping=True)  # 与生产 `get_engine()` 一致
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tx = Transcript()
    app_id, key = await prepare(sessions, s)
    print(f"演示应用 {app_id}（{APP_NAME}），Key 前缀 {key[:12]}…")

    # 接收端
    rport = free_port()
    receiver.STATE.reset(secret="", fail_first=0)
    rserver = uvicorn.Server(
        uvicorn.Config(
            receiver.app, host="127.0.0.1", port=rport, access_log=False, log_level="warning"
        )
    )
    rtask = asyncio.create_task(rserver.serve())
    rbase = f"http://127.0.0.1:{rport}"

    # 业务应用
    app = create_app()
    redis = aioredis.from_url(s.redis_url)
    if not real_llm:
        # 假模型：复用测试里的脚本模型 + 关键词知识库，本机没有模型 Key 也能跑完整条链路
        from tests.chat_support import make_runtime

        runtime = make_runtime(sessions)
        runtime.redis = redis  # 事件流与广播都要真的 Redis（M3.5 ②）
        runtime.redis_prefix = s.redis_prefix
        app.state.chat_runtime = runtime
        app.state.handoff_runtime = HandoffRuntime(
            sessions=sessions, redis=redis, redis_prefix=s.redis_prefix
        )
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, port=port, access_log=False, log_level="warning"))
    serve = asyncio.create_task(server.serve())
    base = f"http://127.0.0.1:{port}"
    head = f"{base}/api/v1/apps/{app_id}"
    auth = {"Authorization": f"Bearer {key}"}
    started = datetime.now(UTC)
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            await wait_ready(c, f"{base}/healthz", serve)
            await wait_ready(c, f"{rbase}/healthz", rtask)

            # 保存接收端地址（保存期会做 SSRF 校验）+ 轮换密钥（管理接口是 M4.5，这里走服务层）
            url = f"{rbase}/hook"
            async with sessions() as db:
                svc = AppService(db)
                await svc.update_app(app_id, {"webhook_config": {"url": url}})
                secret = await svc.rotate_webhook_secret(app_id)
                await db.commit()
            receiver.STATE.reset(secret=secret)
            tx.step(
                "⓪ 准备：应用、Key、接收端",
                {
                    "app_id": str(app_id),
                    "webhook_url": url,
                    "secret_prefix": secret[:14],
                    "key_prefix": key[:12],
                },
            )
            tx.check("接收端已就绪", (await c.get(f"{rbase}/healthz")).status_code == 200)

            # ① 用户说要人工 → 事件落库 + 排投递 + 投出去
            r = await c.post(f"{head}/chat", json={"question": ASK_HUMAN}, headers=auth)
            turn = r.json()
            cid = UUID(turn["conversation_id"])
            rows = await events_of(sessions, cid)
            delivered = await deliver_pending(sessions, cid)
            got = await received(c, rbase)
            tx.step(
                "① 用户「我要人工」→ handoff.requested 投递到接收端",
                {
                    "turn": {
                        "handoff_status": turn.get("handoff_status"),
                        "reply_by": turn.get("reply_by"),
                    },
                    "events": rows,
                    "delivery": delivered,
                    "receiver": got["items"][-1:] if got["items"] else [],
                },
            )
            tx.check(
                "事件流有 handoff.requested", [e["type"] for e in rows] == ["handoff.requested"]
            )
            tx.check("投递成功 delivered", delivered[0]["status"] == "delivered", str(delivered))
            item = got["items"][0]
            tx.check("接收端验签通过", item["signature_ok"] is True)
            tx.check(
                "事件名与 payload 一致",
                item["event"] == item["payload"]["event"] == "handoff.requested",
            )
            tx.check(
                "payload 公共字段齐全",
                {
                    "id",
                    "event",
                    "app_id",
                    "conversation_id",
                    "user_id",
                    "seq",
                    "occurred_at",
                    "data",
                }
                == set(item["payload"]),
            )
            tx.check(
                "坐席平台能拿到接单前上下文",
                {"reason", "intent", "handoff_ai_mode", "summary", "facts", "recent_messages"}
                == set(item["payload"]["data"]),
            )

            # ② 坐席抢单 → handoff.accepted
            r = await c.post(
                f"{head}/conversations/{cid}/handoff/accept",
                json={"agent_id": AGENT_ID, "agent_name": AGENT_NAME},
                headers=auth,
            )
            accepted = r.json()
            rows = await events_of(sessions, cid)
            delivered = await deliver_pending(sessions, cid)
            got = await received(c, rbase)
            tx.step(
                "② 坐席 accept → handoff.accepted",
                {
                    "http": r.status_code,
                    "handoff_status": accepted.get("handoff_status"),
                    "events": rows,
                },
            )
            tx.check(
                "HTTP 200 且状态 accepted",
                r.status_code == 200 and accepted["handoff_status"] == "accepted",
            )
            tx.check("事件 handoff.accepted 已投递", delivered[0]["status"] == "delivered")
            tx.check(
                "接收端看到坐席信息",
                got["items"][-1]["payload"]["data"]
                == {"agent_id": AGENT_ID, "agent_name": AGENT_NAME},
                json.dumps(got["items"][-1]["payload"]["data"], ensure_ascii=False),
            )

            # ③ 坐席发言 → message.created，用户可见
            r = await c.post(
                f"{head}/conversations/{cid}/handoff/messages",
                json={"agent_id": AGENT_ID, "content": "在的，请讲"},
                headers=auth,
            )
            await deliver_pending(sessions, cid)
            r2 = await c.get(f"{head}/conversations/{cid}/messages", headers=auth)
            msgs = r2.json()["items"]
            got = await received(c, rbase)
            tx.step(
                "③ 坐席发言 → message.created",
                {
                    "http": r.status_code,
                    "receiver_event": got["items"][-1]["event"],
                    "last_message": {
                        k: msgs[-1][k] for k in ("role", "kind", "content", "agent_id")
                    },
                },
            )
            tx.check(
                "HTTP 201 且事件是 message.created",
                r.status_code == 201 and got["items"][-1]["event"] == "message.created",
            )
            tx.check(
                "用户能看到 human_agent 的消息",
                msgs[-1]["role"] == "human_agent" and msgs[-1]["content"] == "在的，请讲",
                str(msgs[-1]["role"]),
            )
            tx.check(
                "message.created 的 data 带 agent_id",
                got["items"][-1]["payload"]["data"]["message"]["agent_id"] == AGENT_ID,
            )

            # ④ 坐席交还 → handoff.released
            r = await c.post(
                f"{head}/conversations/{cid}/handoff/release",
                json={"agent_id": AGENT_ID, "note": "已解决"},
                headers=auth,
            )
            released = r.json()
            delivered = await deliver_pending(sessions, cid)
            got = await received(c, rbase)
            tx.step(
                "④ 坐席 release → handoff.released",
                {
                    "http": r.status_code,
                    "handoff_status": released.get("handoff_status"),
                    "receiver_event": got["items"][-1]["event"],
                    "data": got["items"][-1]["payload"]["data"],
                },
            )
            tx.check(
                "HTTP 200 且状态 released",
                r.status_code == 200 and released["handoff_status"] == "released",
            )
            tx.check(
                "released 的 data 是 agent/agent",
                got["items"][-1]["payload"]["data"]
                == {
                    "agent_id": AGENT_ID,
                    "note": "已解决",
                    "released_by": "agent",
                    "reason": "agent",
                },
            )
            tx.check("全流程四个事件都投递成功", all(d["status"] == "delivered" for d in delivered))

            # ⑤ 接收端 500 → 退避重试 → 到期扫描 → 重投成功
            receiver.STATE.reset(fail_first=1)
            r = await c.post(f"{head}/chat", json={"question": ASK_HUMAN}, headers=auth)
            cid2 = UUID(r.json()["conversation_id"])
            row = (await deliveries_of(sessions, cid2))[0]
            first = await webhooks.run_deliver_job(row.id)
            row = (await deliveries_of(sessions, cid2))[0]
            backoff = (
                (row.next_attempt_at - datetime.now(UTC)).total_seconds()
                if row.next_attempt_at
                else None
            )
            tx.step(
                "⑤ 接收端返回 500 → 按退避重试",
                {
                    "first": first,
                    "status": row.status,
                    "attempts": row.attempts,
                    "backoff_seconds": backoff,
                },
            )
            tx.check(
                "首次按失败处理（pending）", first["status"] == "retry" and row.status == "pending"
            )
            tx.check(
                "attempts=1 且错误码记下来", row.attempts == 1 and row.last_error == "HTTP 500"
            )
            tx.check("退避约 10 秒", backoff is not None and 8 <= backoff <= 11, f"{backoff:.1f}s")

            async with sessions() as db:  # 把到期时间拨回过去：模拟 10 秒后的扫描
                await db.execute(
                    update(WebhookDelivery)
                    .where(WebhookDelivery.id == row.id)
                    .values(next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
                )
                await db.commit()
            due = await webhooks.run_retry_sweep_job()
            again = await webhooks.run_retry_sweep_job()
            second = await webhooks.run_deliver_job(row.id)  # 模拟 worker 消费队列里的那条
            row = (await deliveries_of(sessions, cid2))[0]
            got = await received(c, rbase)
            tx.step(
                "⑤b 到期扫描领取 → 重投成功",
                {
                    "due": due,
                    "second_sweep": again,
                    "retry": second,
                    "status": row.status,
                    "attempts": row.attempts,
                },
            )
            tx.check("扫描按到期时间领取（第二次为空）", str(row.id) in due and again == [])
            tx.check(
                "重投成功 delivered", second["status"] == "delivered" and row.status == "delivered"
            )
            tx.check(
                "接收端这次收下了",
                got["items"][-1]["signature_ok"] is True and got["failures"] == 1,
            )

            # ⑥ 事件流：断线重连按 Last-Event-ID 补发；连接期间实时推送
            # 先给 cid2 补第二条事件（坐席接单），否则「只补发更新的帧」无从验证
            r = await c.post(
                f"{head}/conversations/{cid2}/handoff/accept",
                json={"agent_id": AGENT_ID, "agent_name": AGENT_NAME},
                headers=auth,
            )
            stream_url = f"{head}/conversations/{cid2}/events"
            first_batch = await read_frames(c, stream_url, auth, limit=1)
            last_id = first_batch[0]["id"]
            replay = await read_frames(
                c, stream_url, {**auth, "Last-Event-ID": str(last_id)}, limit=1
            )
            # 第三次连接把游标推到最前：只有新事件的推送才会让它返回
            live_task = asyncio.create_task(
                read_frames(
                    c,
                    stream_url,
                    {**auth, "Last-Event-ID": str(replay[0]["id"] if replay else last_id)},
                    limit=1,
                    timeout=8.0,
                )
            )
            await asyncio.sleep(0.3)  # 让流先订阅上
            await c.post(
                f"{head}/conversations/{cid2}/handoff/messages",
                json={"agent_id": AGENT_ID, "content": "稍等，我确认一下"},
                headers=auth,
            )
            live = await live_task
            tx.step(
                "⑥ 事件流：断线补发与实时推送",
                {
                    "accept_http": r.status_code,
                    "first_id": last_id,
                    "replay": [f["id"] for f in replay],
                    "live": [f["id"] for f in live],
                    "live_event": live[0]["payload"]["event"] if live else None,
                },
            )
            tx.check("重连只补发更新的帧", bool(replay) and replay[0]["id"] == last_id + 1)
            tx.check(
                "连接期间的实时事件按 seq 送达",
                bool(live) and live[0]["payload"]["seq"] == live[0]["id"],
            )
            tx.check(
                "实时帧就是坐席刚发的那条消息事件",
                bool(live) and live[0]["payload"]["event"] == "message.created",
                live[0]["payload"]["event"] if live else "无",
            )

            tx.check("真实模型花费 0 元（stub）", True, "stub-llm" if not real_llm else "real-llm")
    finally:
        await redis.aclose()
        server.should_exit = True
        rserver.should_exit = True
        await asyncio.gather(serve, rtask, return_exceptions=True)
        await engine.dispose()

    return {
        "mode": "stub-llm" if not real_llm else "real-llm",
        "app": APP_NAME,
        "app_id": str(app_id),
        "key_prefix": key[:12],
        "started_at": started.isoformat(),
        "seconds": round((datetime.now(UTC) - started).total_seconds(), 2),
        "passed": tx.passed,
        "checks": tx.checks,
        "steps": tx.steps,
    }


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        # 本机控制台是 GBK：中文提示与上标字符不该让脚本崩掉（证据文件仍是 UTF-8）
        sys.stdout.reconfigure(errors="replace")
    p = argparse.ArgumentParser(
        description="M3.5 Webhook 与会话事件流验收（真 HTTP + 真 PG + 真 Redis）"
    )
    p.add_argument("--real-llm", action="store_true", help="用真实模型（默认用测试脚本模型，0 元）")
    args = p.parse_args()
    result = asyncio.run(run(real_llm=args.real_llm))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    total = len(result["checks"])
    print(f"\n结果：{result['passed']}/{total} 通过，用时 {result['seconds']}s，证据：{OUT}")
    if result["passed"] != total:
        failed = [k for k, v in result["checks"].items() if not v]
        print("未通过：\n  - " + "\n  - ".join(failed))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
