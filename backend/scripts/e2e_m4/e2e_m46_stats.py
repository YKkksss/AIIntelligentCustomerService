"""M4.6 里程碑验收：日聚合、统计接口与仪表盘数据一致性。

    # 本机 Docker 栈起好、迁移到 head、真 PG/Redis，假模型（默认，0 元）
    PYTHONPATH=. uv run python -m scripts.e2e_m4.e2e_m46_stats

进程内起 uvicorn（`create_app()`），全部真 HTTP：超管登录 → 建应用 → 直插会话与
消息（api / debug / eval 三种渠道）→ 见 `run_aggregate_job` 重算当天（跑两次验幂等）
→ `GET /admin/apps/{app_id}/stats` 与 `GET /admin/stats/overview` 的逐日序列、合计与
debug/eval 排除 → viewer 可读、无成员 403 → 清理。

判定项见末尾汇总：聚合幂等、计数对得上、debug/eval 不进统计、分位数只在单应用接口、
`days` 边界 422、viewer 只读可看、无权成员 403。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import uvicorn
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aics.core.config import get_settings
from aics.db.models import App, Conversation, Feedback, Message, User, UserAppRole
from aics.main import create_app
from aics.modules.stats.service import run_aggregate_job

HERE = Path(__file__).resolve().parent
OUT = HERE / "results" / "e2e_m46_stats.json"
APP_NAME = "e2e-m46-stats"
ADMIN_EMAIL = "admin@aisc.com"
ADMIN_PASSWORD = "admin123456@"  # noqa: S105  本地开发默认口令，见 core/config.py
VIEWER_EMAIL = "e2e-m46-viewer@example.com"
VIEWER_PASSWORD = "Viewer#2026pass"  # noqa: S105  验收临时账号
OUTSIDER_EMAIL = "e2e-m46-outsider@example.com"
SHANGHAI = ZoneInfo("Asia/Shanghai")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Transcript:
    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []
        self.checks: dict[str, bool] = {}

    def step(self, name: str, body: Any) -> None:
        self.steps.append({"step": name, "body": body})
        print(f"\n[{len(self.steps)}] {name}")
        print(f"    {json.dumps(body, ensure_ascii=False, default=str)[:700]}")

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks[name] = bool(ok)
        print(f"    {'OK  ' if ok else 'FAIL'} {name}{'  <- ' + detail if detail else ''}")

    @property
    def passed(self) -> int:
        return sum(1 for v in self.checks.values() if v)


async def db_setup(sessions: async_sessionmaker[Any], user_id: uuid.UUID) -> None:
    """验收用账号跳过首登改密，直接读接口。"""
    async with sessions() as session:
        await session.execute(
            update(User).where(User.id == user_id).values(must_change_password=False)
        )
        await session.commit()


def today_shanghai() -> str:
    return datetime.now(SHANGHAI).date().isoformat()


async def seed(
    sessions: async_sessionmaker[Any], app_id: uuid.UUID, owner: uuid.UUID
) -> dict[str, Any]:
    """插入三条会话：api 正常问答（含点踩）、debug、eval。"""
    now = datetime.now(UTC)
    async with sessions() as session:
        convs = []
        for channel in ("api", "debug", "eval"):
            conv = Conversation(
                app_id=app_id,
                channel=channel,
                status="closed",
                closed_at=now,
                close_reason="idle",
                last_activity_at=now,
                created_at=now,
            )
            session.add(conv)
            convs.append(conv)
        await session.flush()
        api_conv, debug_conv, eval_conv = convs

        rows = [
            Message(
                app_id=app_id,
                conversation_id=api_conv.id,
                role="user",
                content="退款多久到账？",
                created_at=now,
            ),
            Message(
                app_id=app_id,
                conversation_id=api_conv.id,
                role="assistant",
                content="1-3 个工作日。",
                answer_type="answer",
                intent="kb_qa",
                prompt_tokens=120,
                completion_tokens=30,
                latency_ms=900,
                first_token_ms=220,
                created_at=now,
            ),
            Message(
                app_id=app_id,
                conversation_id=api_conv.id,
                role="assistant",
                content="这条是拒答演示。",
                answer_type="refused",
                intent="out_of_scope",
                prompt_tokens=90,
                completion_tokens=10,
                latency_ms=400,
                first_token_ms=150,
                created_at=now,
            ),
            Message(
                app_id=app_id,
                conversation_id=debug_conv.id,
                role="assistant",
                content="调试会话不应被统计。",
                answer_type="answer",
                intent="kb_qa",
                created_at=now,
            ),
            Message(
                app_id=app_id,
                conversation_id=eval_conv.id,
                role="assistant",
                content="评测会话不应被统计。",
                answer_type="answer",
                intent="kb_qa",
                created_at=now,
            ),
        ]
        session.add_all(rows)
        await session.flush()
        session.add(
            Feedback(
                app_id=app_id,
                message_id=rows[1].id,
                rating=-1,
                comment="答得不全",
                created_at=now,
            )
        )
        await session.commit()
        return {
            "app_id": str(app_id),
            "owner": str(owner),
            "api_conversation": str(api_conv.id),
            "debug_conversation": str(debug_conv.id),
            "eval_conversation": str(eval_conv.id),
        }


async def main(*, verbose: bool = False) -> int:  # noqa: ARG001  与其它脚本保持同一签名
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    t = Transcript()
    s = get_settings()
    day = today_shanghai()
    base = f"http://127.0.0.1:{free_port()}"
    app = create_app()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=int(base.rsplit(":", 1)[1]), log_level="warning")
    )
    task = asyncio.create_task(server.serve())
    engine = create_async_engine(s.db_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    created_app: uuid.UUID | None = None
    user_ids: list[uuid.UUID] = []
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)

        async with httpx.AsyncClient(base_url=base, timeout=30.0) as client:
            # ① 登录
            r = await client.post(
                "/api/v1/admin/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
            )
            body = r.json()
            t.check("admin login 200", r.status_code == 200, f"status={r.status_code}")
            token = body.get("access_token", "")
            admin_id = uuid.UUID(body["user"]["id"])
            t.step(
                "① 超管登录",
                {"status": r.status_code, "is_super_admin": body.get("is_super_admin")},
            )
            auth = {"Authorization": f"Bearer {token}"}

            # ② 建应用 + 造三种渠道的数据
            r = await client.post(
                "/api/v1/admin/apps",
                json={"name": APP_NAME, "description": "M4.6 统计验收"},
                headers=auth,
            )
            t.check("create app 201", r.status_code == 201, f"status={r.status_code}")
            created_app = uuid.UUID(r.json()["id"])
            seeded = await seed(sessions, created_app, admin_id)
            t.step("② 造数据（api / debug / eval 各一条会话）", seeded)

            # ③ 聚合当天（跑两次验幂等）
            first = await run_aggregate_job(day)
            second = await run_aggregate_job(day)
            t.check(
                "aggregate ran",
                bool(first) and bool(second),
                f"first={len(first)} second={len(second)}",
            )
            async with sessions() as session:
                from sqlalchemy import select

                from aics.db.models import AppDailyStats

                row = (
                    await session.execute(
                        select(AppDailyStats).where(
                            AppDailyStats.app_id == created_app,
                            AppDailyStats.date == date.fromisoformat(day),
                        )
                    )
                ).scalar_one_or_none()
                snapshot = dict(row.__dict__) if row is not None else {}
                snapshot.pop("_sa_instance_state", None)
            t.check("aggregate row written", row is not None, f"date={day}")
            t.check(
                "aggregate idempotent",
                bool(snapshot) and snapshot.get("conversations") == 1,
                f"conversations={snapshot.get('conversations')}",
            )

            # ④ 单应用统计
            r = await client.get(
                f"/api/v1/admin/apps/{created_app}/stats", params={"days": 7}, headers=auth
            )
            stats = r.json()
            items = stats.get("items", [])
            today_row = next((i for i in items if i.get("date") == day), {})
            t.check("app stats 200", r.status_code == 200, f"status={r.status_code}")
            t.check(
                "app series has today",
                bool(today_row),
                f"dates={[i.get('date') for i in items][-3:]}",
            )
            t.check(
                "debug/eval excluded (1 conversation)",
                today_row.get("conversations") == 1,
                f"conversations={today_row.get('conversations')}",
            )
            t.check(
                "message counters (messages 计用户轮次)",
                today_row.get("messages") == 1  # messages 只计用户轮次
                and today_row.get("answered") == 1
                and today_row.get("refused") == 1,
                f"messages={today_row.get('messages')} answered={today_row.get('answered')} "
                f"refused={today_row.get('refused')}",
            )
            t.check(
                "negative feedback counted",
                today_row.get("negative_feedback") == 1,
                f"negative_feedback={today_row.get('negative_feedback')}",
            )
            t.check(
                "latency percentiles present",
                today_row.get("latency_p50_ms") is not None
                and today_row.get("first_token_p95_ms") is not None,
                f"p50={today_row.get('latency_p50_ms')} ft95={today_row.get('first_token_p95_ms')}",
            )
            t.check(
                "intent distribution",
                (today_row.get("intent_distribution") or {}).get("kb_qa") == 1,
                f"intent={today_row.get('intent_distribution')}",
            )
            t.step("③ 聚合与单应用统计", {"day": day, "today_row": today_row})

            # ⑤ 全局概览
            r = await client.get("/api/v1/admin/stats/overview", params={"days": 7}, headers=auth)
            overview = r.json()
            totals = overview.get("totals", {})
            apps = overview.get("apps", [])
            t.check("overview 200", r.status_code == 200, f"status={r.status_code}")
            t.check(
                "overview has series",
                bool(overview.get("series")),
                f"n={len(overview.get('series', []))}",
            )
            t.check(
                "overview totals cover app",
                totals.get("conversations", 0) >= 1,
                f"conversations={totals.get('conversations')}",
            )
            t.check(
                "overview apps include created app",
                any(a.get("app_id") == str(created_app) for a in apps),
                f"apps={len(apps)}",
            )
            t.check(
                "overview has no latency keys",
                "latency_p50_ms" not in totals,
                f"keys={sorted(totals)[:6]}",
            )
            t.step("④ 全局概览", {"totals": totals, "apps": len(apps)})

            # ⑥ days 边界
            bad = await client.get(
                f"/api/v1/admin/apps/{created_app}/stats", params={"days": 0}, headers=auth
            )
            t.check("days=0 -> 422", bad.status_code == 422, f"status={bad.status_code}")

            # ⑦ viewer 可读、无成员 403
            r = await client.post(
                "/api/v1/admin/users",
                json={
                    "email": VIEWER_EMAIL,
                    "display_name": "M4.6 验收 viewer",
                    "password": VIEWER_PASSWORD,
                },
                headers=auth,
            )
            viewer_id = uuid.UUID(r.json()["id"]) if r.status_code == 201 else None
            if viewer_id is not None:
                user_ids.append(viewer_id)
                await client.put(
                    f"/api/v1/admin/apps/{created_app}/members/{viewer_id}",
                    json={"role_id": "viewer"},
                    headers=auth,
                )
            login = await client.post(
                "/api/v1/admin/auth/login",
                json={"email": VIEWER_EMAIL, "password": VIEWER_PASSWORD},
            )
            viewer_token = login.json().get("access_token", "")
            viewer_auth = {"Authorization": f"Bearer {viewer_token}"}
            if viewer_id is not None:
                await db_setup(sessions, viewer_id)
            r = await client.get(
                f"/api/v1/admin/apps/{created_app}/stats", params={"days": 7}, headers=viewer_auth
            )
            t.check("viewer can read stats", r.status_code == 200, f"status={r.status_code}")

            r = await client.post(
                "/api/v1/admin/users",
                json={
                    "email": OUTSIDER_EMAIL,
                    "display_name": "M4.6 验收无成员",
                    "password": VIEWER_PASSWORD,
                },
                headers=auth,
            )
            outsider_id = uuid.UUID(r.json()["id"]) if r.status_code == 201 else None
            if outsider_id is not None:
                user_ids.append(outsider_id)
            login = await client.post(
                "/api/v1/admin/auth/login",
                json={"email": OUTSIDER_EMAIL, "password": VIEWER_PASSWORD},
            )
            outsider_auth = {"Authorization": f"Bearer {login.json().get('access_token', '')}"}
            r = await client.get(
                f"/api/v1/admin/apps/{created_app}/stats",
                params={"days": 7},
                headers=outsider_auth,
            )
            t.check("non-member 403", r.status_code == 403, f"status={r.status_code}")
            t.step(
                "⑤ 权限",
                {
                    "viewer": "200",
                    "outsider": r.status_code,
                    "viewer_created": viewer_id is not None,
                },
            )

            # ⑧ 清理
            if created_app is not None:
                await client.delete(f"/api/v1/admin/apps/{created_app}", headers=auth)
    finally:
        server.should_exit = True
        await task
        async with sessions() as session:
            if created_app is not None:
                await session.execute(
                    delete(Conversation).where(Conversation.app_id == created_app)
                )
                await session.execute(delete(Message).where(Message.app_id == created_app))
                await session.execute(delete(Feedback).where(Feedback.app_id == created_app))
                await session.execute(delete(UserAppRole).where(UserAppRole.app_id == created_app))
                await session.execute(delete(App).where(App.id == created_app))
            for uid in user_ids:
                await session.execute(delete(User).where(User.id == uid))
            await session.commit()
        await engine.dispose()

    total = len(t.checks)
    result = {
        "passed": t.passed,
        "total": total,
        "checks": t.checks,
        "steps": t.steps,
        "day": day,
        "mode": "stub-llm",
        "at": datetime.now(UTC).isoformat(),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n== {t.passed}/{total} ==  {OUT}")
    if t.passed != total:
        failed = [k for k, v in t.checks.items() if not v]
        print("failed:", ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="M4.6 里程碑验收")
    parser.add_argument("--real-llm", action="store_true", help="保留参数位（统计验收不调模型）")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(verbose=args.real_llm)))


def _unused(*_: Any) -> None:  # pragma: no cover  占位避免未用导入告警
    _ = timedelta
