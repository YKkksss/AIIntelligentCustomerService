"""M4.5 里程碑验收：Key 管理、会话日志、强制释放、待回访、反馈与 Webhook。

    # 本机 Docker 栈起好、迁移到 head、真 PG/Redis，假模型（默认，0 元）
    PYTHONPATH=. uv run python -m scripts.e2e_m4.e2e_m45_ops

脚本在进程内起 uvicorn（`create_app()`）并换成 `tests/chat_support.make_runtime` 的脚本
模型；除模型外全部真实：鉴权 / Redis 限流与缓存 / PG / 事件 outbox / Webhook 投递。
Webhook 接收端挂在同一个 FastAPI 实例的 `/__webhook_probe`，SSRF 允许名单用
`settings.model_copy` 放行 127.0.0.1（本机验收专用）。

判定项见末尾汇总：创建只回一次明文、吊销后立即 401（缓存失效）、日志筛选与全文检索、
强制释放的状态迁移与二次冲突、待回访标记、反馈标记已处理、Webhook 签名投递 / 重发 /
测试事件 / 密钥轮换，以及每一步的审计动作。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests.chat_support import make_runtime

from aics.core import secretbox
from aics.core.config import get_settings
from aics.db.models import (
    ApiKey,
    App,
    AuditLog,
    Conversation,
    ConversationEvent,
    Feedback,
    Job,
    User,
    UserAppRole,
    WebhookDelivery,
)
from aics.main import build_webhooks_admin_runtime, create_app
from aics.modules.apps.service import AppService
from aics.modules.audit.sink import PgAuditSink
from aics.modules.webhooks.service import WebhookService

HERE = Path(__file__).resolve().parent
OUT = HERE / "results" / "e2e_m45_ops.json"
APP_NAME = "e2e-m45-ops"
ADMIN_EMAIL = "admin@aisc.com"
ADMIN_PASSWORD = "admin123456@"  # noqa: S105 — 本地开发默认口令，见 core/config.py
PROBE = "/__webhook_probe"

_received: list[dict[str, Any]] = []
_probe_fail = False


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
        print(f"    {'OK  ' if ok else 'FAIL'} {name}{'  → ' + detail if detail else ''}")

    @property
    def passed(self) -> int:
        return sum(1 for v in self.checks.values() if v)


def verify(secret: str, headers: dict[str, str], body: bytes) -> bool:
    ts = headers.get("x-aics-timestamp", "")
    signature = headers.get("x-aics-signature", "")
    expected = (
        "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    )
    return hmac.compare_digest(expected, signature)


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


async def prepare(sessions: Any) -> tuple[uuid.UUID, uuid.UUID]:
    """重建演示应用，返回 (app_id, admin_id)。留着上轮数据会让筛选用例不稳定。"""
    async with sessions() as db:
        old = select(App.id).where(App.name == APP_NAME)
        await db.execute(delete(AuditLog).where(AuditLog.app_id.in_(old)))
        await db.execute(delete(WebhookDelivery).where(WebhookDelivery.app_id.in_(old)))
        await db.execute(delete(ConversationEvent).where(ConversationEvent.app_id.in_(old)))
        await db.execute(delete(Feedback).where(Feedback.app_id.in_(old)))
        await db.execute(delete(Conversation).where(Conversation.app_id.in_(old)))
        await db.execute(delete(ApiKey).where(ApiKey.app_id.in_(old)))
        await db.execute(delete(Job).where(Job.app_id.in_(old)))
        await db.execute(delete(UserAppRole).where(UserAppRole.app_id.in_(old)))
        await db.execute(delete(App).where(App.name == APP_NAME))

        admin = (await db.execute(select(User).where(User.email == ADMIN_EMAIL))).scalar_one()
        app = await AppService(db).create_app(name=APP_NAME, created_by=admin.id)
        await db.commit()
    return app.id, admin.id


async def login(client: httpx.AsyncClient, email: str, password: str) -> str:
    resp = await client.post(
        "/api/v1/admin/auth/login", json={"email": email, "password": password}
    )
    resp.raise_for_status()
    return str(resp.json()["access_token"])


async def run() -> dict[str, Any]:
    s = get_settings()
    t = Transcript()
    engine = create_async_engine(s.db_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    app_id, admin_id = await prepare(sessions)
    await bootstrap_admin()

    application = create_app()

    @application.post(PROBE)
    async def _probe(request: Request) -> JSONResponse:  # noqa: ANN202 - 验收用接收端
        raw = await request.body()
        _received.append(
            {
                "event": request.headers.get("x-aics-event", ""),
                "delivery": request.headers.get("x-aics-delivery", ""),
                "body": raw.decode("utf-8", "replace"),
                "headers": {k.lower(): v for k, v in request.headers.items()},
            }
        )
        return JSONResponse({"ok": True}, status_code=500 if _probe_fail else 200)

    application.state.chat_runtime = make_runtime(sessions)
    # 本机接收端在 127.0.0.1：放行 SSRF 校验（仅本脚本的运行时设置）。
    webhook_settings = s.model_copy(update={"url_fetch_allowlist": ("127.0.0.1",)})
    application.state.webhooks_admin_runtime = build_webhooks_admin_runtime(
        webhook_settings, audit=PgAuditSink()
    )

    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            application, host="127.0.0.1", port=port, access_log=False, log_level="warning"
        )
    )
    task = asyncio.create_task(server.serve())
    base = f"http://127.0.0.1:{port}"

    try:
        async with httpx.AsyncClient(base_url=base, timeout=30) as client:
            await _wait_ready(client)
            token = await login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
            headers = {"Authorization": f"Bearer {token}"}
            prefix = f"/api/v1/admin/apps/{app_id}"

            # ① 应用与 Key：创建只回一次明文
            created = await client.post(
                f"{prefix}/keys",
                json={"name": "验收 Key", "scopes": ["chat"], "rpm_limit": 30},
                headers=headers,
            )
            key_body = created.json()
            t.step(
                "① 创建 Key",
                {"status": created.status_code, "prefix": key_body.get("prefix")},
            )
            t.check("创建返回 201", created.status_code == 201, str(created.status_code))
            plaintext = key_body.get("key") or ""
            t.check("响应含一次性明文 key", plaintext.startswith("aics_"), plaintext[:10])
            key_id = key_body.get("id", "")

            listing = (await client.get(f"{prefix}/keys", headers=headers)).json()
            t.check(
                "列表不回传明文",
                all("key" not in row for row in listing.get("items", [])),
                str(list(listing.get("items", [{}])[0].keys())[:6]),
            )

            # ② 用 Key 调开放接口，再吊销 → 缓存立即失效
            key_headers = {"Authorization": f"Bearer {plaintext}"}
            first = await client.post(
                f"/api/v1/apps/{app_id}/chat",
                json={"question": "我想申请退款，流程是什么"},
                headers=key_headers,
            )
            t.step("② Key 调用开放接口", {"status": first.status_code})
            t.check("Key 可调用 /chat", first.status_code == 200, str(first.status_code))
            conversation_id = first.json().get("conversation_id") or ""

            revoked = await client.delete(f"{prefix}/keys/{key_id}", headers=headers)
            t.check("吊销返回 200", revoked.status_code == 200, str(revoked.status_code))
            after = await client.post(
                f"/api/v1/apps/{app_id}/chat",
                json={"question": "还在吗"},
                headers=key_headers,
            )
            err = after.json().get("error", {})
            t.step("② 吊销后调用", {"status": after.status_code, "code": err.get("code")})
            t.check(
                "吊销后立即 401（缓存已删）",
                after.status_code == 401 and err.get("code") == "API_KEY_REVOKED",
                f"{after.status_code}/{err.get('code')}",
            )

            key2 = await client.post(
                f"{prefix}/keys",
                json={"name": "验收 Key 2", "scopes": ["chat"]},
                headers=headers,
            )
            key2_headers = {"Authorization": f"Bearer {key2.json()['key']}"}
            t.check("第二个 Key 创建成功", key2.status_code == 201, str(key2.status_code))

            # ③ 日志筛选与详情
            page = (
                await client.get(
                    f"{prefix}/conversations?channel=api&limit=10&q=退款", headers=headers
                )
            ).json()
            t.step("③ 日志筛选", {"total": len(page.get("items", []))})
            t.check(
                "关键字命中会话",
                any(row["id"] == conversation_id for row in page.get("items", [])),
                f"items={len(page.get('items', []))}",
            )

            detail = (
                await client.get(f"{prefix}/conversations/{conversation_id}", headers=headers)
            ).json()
            roles = [m["role"] for m in detail.get("messages", [])]
            t.check(
                "详情含 user/assistant 消息",
                roles[:2] == ["user", "assistant"],
                str(roles[:3]),
            )
            t.check("详情不带摘要时报空", detail.get("summary") is None, str(detail.get("summary")))

            # ④ 强制释放：pending → released，二次 409
            async with sessions() as db:
                await db.execute(
                    update(Conversation)
                    .where(Conversation.id == uuid.UUID(conversation_id))
                    .values(
                        handoff_status="pending",
                        handoff_reason="user_request",
                        handoff_requested_at=datetime.now(UTC),
                    )
                )
                await db.commit()
            released = await client.post(
                f"{prefix}/conversations/{conversation_id}/release",
                json={"note": "验收强制释放"},
                headers=headers,
            )
            body = released.json()
            t.step("④ 强制释放", {"status": released.status_code})
            t.check(
                "释放后 handoff_status=released",
                released.status_code == 200 and body.get("handoff_status") == "released",
                f"{released.status_code}/{body.get('handoff_status')}",
            )
            t.check(
                "释放记录结束原因为 admin",
                body.get("handoff_end_reason") == "admin",
                str(body.get("handoff_end_reason")),
            )
            again = await client.post(
                f"{prefix}/conversations/{conversation_id}/release", json={}, headers=headers
            )
            err = again.json().get("error", {})
            t.check(
                "二次释放 409",
                again.status_code == 409 and err.get("code") == "HANDOFF_STATE_CONFLICT",
                f"{again.status_code}/{err.get('code')}",
            )

            # ⑤ 待回访：列表 → 标记已回访 → 二次 409
            async with sessions() as db:
                await db.execute(
                    update(Conversation)
                    .where(Conversation.id == uuid.UUID(conversation_id))
                    .values(
                        needs_callback=True,
                        callback_reason="queue_timeout",
                        callback_requested_at=datetime.now(UTC),
                    )
                )
                await db.commit()
            callbacks = (await client.get(f"{prefix}/callbacks", headers=headers)).json()
            t.check(
                "待回访列表含该会话",
                any(row["id"] == conversation_id for row in callbacks.get("items", [])),
                f"items={len(callbacks.get('items', []))}",
            )
            cleared = await client.post(
                f"{prefix}/callbacks/{conversation_id}",
                json={"note": "已回电"},
                headers=headers,
            )
            t.check(
                "标记已回访并记录备注",
                cleared.status_code == 200
                and cleared.json().get("needs_callback") is False
                and cleared.json().get("callback_note") == "已回电",
                str(cleared.json().get("callback_note")),
            )
            twice = await client.post(
                f"{prefix}/callbacks/{conversation_id}", json={}, headers=headers
            )
            t.check(
                "二次标记 409",
                twice.status_code == 409
                and twice.json().get("error", {}).get("code") == "CALLBACK_STATE_CONFLICT",
                str(twice.status_code),
            )

            # ⑥ 反馈：写一条点踩 → 列表 → 标记已处理
            messages = detail.get("messages", [])
            answer_id = next(m["id"] for m in messages if m["role"] == "assistant")
            feedback = await client.post(
                f"/api/v1/apps/{app_id}/messages/{answer_id}/feedback",
                json={"rating": -1, "comment": "答非所问"},
                headers=key2_headers,
            )
            t.step("⑥ 提交点踩", {"status": feedback.status_code})
            t.check("点踩写入 200", feedback.status_code == 200, str(feedback.status_code))
            # Key 已吊销，改用另一个 Key 只为该步；直接复用刚才吊销前的会话行即可。
            fb_page = (
                await client.get(f"{prefix}/feedback?rating=-1&handled=false", headers=headers)
            ).json()
            rows = fb_page.get("items", [])
            t.check(
                "反馈列表含该条且未处理",
                bool(rows) and rows[0]["handled"] is False,
                f"items={len(rows)}",
            )
            handled = await client.patch(
                f"{prefix}/feedback/{rows[0]['id']}", json={"handled": True}, headers=headers
            )
            t.check(
                "标记已处理",
                handled.status_code == 200 and handled.json().get("handled") is True,
                str(handled.json().get("handled")),
            )

            # ⑦ Webhook：写配置 → 发测试事件验签 → 轮换密钥
            secret = secretbox.new_secret()
            async with sessions() as db:
                app = await db.get(App, app_id)
                if app is None:
                    raise RuntimeError("应用缺失")
                app.webhook_config = {
                    **dict(app.webhook_config or {}),
                    "url": f"{base}{PROBE}",
                    "secret_enc": secretbox.encrypt(
                        secret, s.data_encryption_key.get_secret_value()
                    ),
                    "secret_prefix": secretbox.prefix_of(secret),
                }
                await db.commit()
            config = (await client.get(f"{prefix}/webhook", headers=headers)).json()
            t.step("⑦ Webhook 配置", {"ready": config.get("ready")})
            t.check("配置就绪且不回传密文", config.get("ready") is True and "secret" not in config)

            _received.clear()
            tested = await client.post(f"{prefix}/webhook/test", headers=headers)
            test_body = tested.json()
            t.step("⑦ 发送测试事件", {"status": tested.status_code, **test_body})
            t.check(
                "测试事件 200 且收到 ping",
                tested.status_code == 200
                and test_body.get("ok") is True
                and _received
                and _received[0]["event"] == "ping",
                f"{test_body.get('status_code')}/{len(_received)}",
            )
            t.check(
                "签名可用共享密钥校验",
                bool(_received)
                and verify(secret, _received[0]["headers"], _received[0]["body"].encode()),
                "hmac",
            )

            rotated = await client.post(f"{prefix}/webhook/secret", headers=headers)
            rotated_body = rotated.json()
            t.check(
                "轮换密钥返回新明文与前缀",
                rotated_body.get("secret", "").startswith("whsec_")
                and rotated_body.get("secret_prefix") != secretbox.prefix_of(secret),
                str(rotated_body.get("secret_prefix")),
            )

            # ⑧ 投递：失败进入退避 → 命中上限转 failed → 重发成功
            global _probe_fail
            _probe_fail = True
            turn = await client.post(
                f"/api/v1/apps/{app_id}/chat",
                json={"question": "我要转人工处理退款"},
                headers=key_headers,
            )
            t.check("吊销后的 Key 仍被拒", turn.status_code == 401, str(turn.status_code))
            # 转人工会写 system 消息与 handoff 事件，正好驱动一次真实投递（§7.3）。
            second = await client.post(
                f"/api/v1/apps/{app_id}/chat",
                json={"question": "我要转人工处理退款"},
                headers=key2_headers,
            )
            t.check("新 Key 可调用", second.status_code == 200, str(second.status_code))
            deliveries: list[dict[str, Any]] = []
            for _ in range(40):
                deliveries = (
                    (await client.get(f"{prefix}/webhook-deliveries?limit=20", headers=headers))
                    .json()
                    .get("items", [])
                )
                if deliveries:
                    break
                await asyncio.sleep(0.25)
            t.step("⑧ 投递记录", {"count": len(deliveries), "first": deliveries[:1]})
            t.check("事件产生投递记录", bool(deliveries), f"count={len(deliveries)}")
            t.check(
                "记录不含 payload",
                all("payload" not in row for row in deliveries),
                "fields",
            )

            first_id = uuid.UUID(deliveries[0]["id"])
            async with sessions() as db:
                await WebhookService(db, settings=webhook_settings).deliver(first_id)
            async with sessions() as db:
                row = await db.get(WebhookDelivery, first_id)
                if row is None:
                    raise RuntimeError("投递行缺失")
                t.step("⑧ 首次失败", {"status": row.status, "attempts": row.attempts})
                t.check(
                    "500 → 记一次失败并排下次重试",
                    row.status == "pending"
                    and row.attempts == 1
                    and row.next_attempt_at is not None,
                    f"{row.status}/{row.attempts}",
                )
                # 模拟退避已耗尽：把 attempts 推到上限前一次，再投一次就转 failed。
                row.attempts = 5
                await db.commit()
            async with sessions() as db:
                await WebhookService(db, settings=webhook_settings).deliver(first_id)
            async with sessions() as db:
                row = await db.get(WebhookDelivery, first_id)
                if row is None:
                    raise RuntimeError("投递行缺失")
                t.step("⑧ 退避耗尽", {"status": row.status, "attempts": row.attempts})
                t.check(
                    "达到 6 次后转 failed",
                    row.status == "failed" and row.attempts == 6,
                    f"{row.status}/{row.attempts}",
                )

            _probe_fail = False
            redeliver = await client.post(
                f"{prefix}/webhook-deliveries/{first_id}/redeliver", headers=headers
            )
            t.step("⑧ 重发", {"status": redeliver.status_code})
            t.check(
                "重发复位为 pending 并把 attempts 归零",
                redeliver.status_code == 200
                and redeliver.json().get("status") == "pending"
                and redeliver.json().get("attempts") == 0,
                str(redeliver.json().get("status")),
            )
            async with sessions() as db:
                await WebhookService(db, settings=webhook_settings).deliver(first_id)
            async with sessions() as db:
                row2 = await db.get(WebhookDelivery, first_id)
                if row2 is None:
                    raise RuntimeError("投递行缺失")
                t.check(
                    "重发后投递成功",
                    row2.status == "delivered" and row2.attempts == 1,
                    f"{row2.status}/{row2.attempts}",
                )

            # ⑨ 审计动作齐全
            async with sessions() as db:
                actions = (
                    (
                        await db.execute(
                            select(AuditLog.action)
                            .where(AuditLog.app_id == app_id)
                            .order_by(AuditLog.id)
                        )
                    )
                    .scalars()
                    .all()
                )
            wanted = {
                "key.create",
                "key.revoke",
                "chat.conversation.release",
                "chat.callback.clear",
                "feedback.handle",
                "webhook.test",
                "webhook.secret",
                "webhook.redeliver",
            }
            t.step("⑨ 审计动作", {"actions": sorted(set(actions))})
            t.check(
                "八个审计动作全部落库",
                wanted <= set(actions),
                str(sorted(wanted - set(actions))),
            )
    finally:
        server.should_exit = True
        await task
        await engine.dispose()
        async with sessions() as db:
            old = select(App.id).where(App.name == APP_NAME)
            await db.execute(delete(AuditLog).where(AuditLog.app_id.in_(old)))
            await db.execute(delete(WebhookDelivery).where(WebhookDelivery.app_id.in_(old)))
            await db.execute(delete(ConversationEvent).where(ConversationEvent.app_id.in_(old)))
            await db.execute(delete(Feedback).where(Feedback.app_id.in_(old)))
            await db.execute(delete(Conversation).where(Conversation.app_id.in_(old)))
            await db.execute(delete(ApiKey).where(ApiKey.app_id.in_(old)))
            await db.execute(delete(Job).where(Job.app_id.in_(old)))
            await db.execute(delete(UserAppRole).where(UserAppRole.app_id.in_(old)))
            await db.execute(delete(App).where(App.name == APP_NAME))
            await db.commit()

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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    result = asyncio.run(run())
    raise SystemExit(0 if result["passed"] == result["total"] else 1)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONPATH", ".")
    main()
