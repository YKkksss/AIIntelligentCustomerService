"""M4.5 ④ (integration): Webhook 管理接口 over real PG.

What only a real database can prove:
- 配置读取把密文藏在 `secret_prefix` 后面；
- 投递记录按状态筛选，且不回传 `payload`；
- 人工重发真的把行复位成 `pending` 并写 `audit_logs`，重复重发 `pending` 行 404；
- 「发送测试」走真实 HTTP 通道（这里注入假 sender）且不落投递表；
- 轮换密钥把密文写回 JSONB，明文只返回一次；
- 跨应用访问别家的投递记录 404。
"""

from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import delete, select, update

from aics.core import secretbox
from aics.core.config import get_settings
from aics.core.errors import ForbiddenError, UnauthenticatedError
from aics.core.security import AuthedUser
from aics.db.models import App, AuditLog, ConversationEvent, WebhookDelivery
from aics.main import create_app
from aics.modules.audit.sink import PgAuditSink
from aics.modules.chat.repository import ConversationRepository
from aics.modules.chat.schemas import NewConversation
from aics.modules.webhooks import admin_service as admin_svc
from aics.modules.webhooks import service as wh
from aics.modules.webhooks.admin_runtime import WebhookAdminRuntime

pytestmark = pytest.mark.integration

HEADERS = {"authorization": "Bearer admin"}
USER_ID = uuid.uuid4()
URL = "https://receiver.example/aics"


class FakeAuthorizer:
    def __init__(self, *, actor_id: UUID = USER_ID, deny: str | None = None) -> None:
        self.deny = deny
        self.actor = AuthedUser(
            id=actor_id, email="admin@example.com", display_name="管理员", is_super_admin=True
        )

    async def authenticate(self, authorization: str | None) -> AuthedUser:
        if authorization != "Bearer admin":
            raise UnauthenticatedError(details={"reason": "missing_token"})
        return self.actor

    async def authorize(self, user: Any, *, app_id: Any, permission: str) -> None:
        if self.deny is not None and permission == self.deny:
            raise ForbiddenError(details={"reason": "missing_permission", "permission": permission})


class AuditRuntime:
    def sink(self) -> PgAuditSink:
        return PgAuditSink()


def _webhook_config(url: str = URL) -> dict[str, Any]:
    plain = secretbox.new_secret()
    key = get_settings().data_encryption_key.get_secret_value()
    return {
        "url": url,
        "secret_enc": secretbox.encrypt(plain, key),
        "secret_prefix": secretbox.prefix_of(plain),
    }


@pytest.fixture
async def ops(pg: dict[str, Any], monkeypatch: Any) -> Any:
    calls: list[dict[str, Any]] = []

    async def sender(url: str, body: bytes, headers: dict[str, str]) -> int:
        calls.append({"url": url, "body": body, "headers": headers})
        return 204

    real_service = wh.WebhookService

    def factory(session: Any, *, settings: Any, repo: Any = None) -> Any:
        return real_service(session, settings=settings, repo=repo, sender=sender)

    monkeypatch.setattr(admin_svc, "WebhookService", factory)
    real_check = wh.netguard.check_url

    def check(url: str, allow: Any, resolver: Any = None) -> list[str]:
        if "receiver.example" in url:
            return ["93.184.216.34"]
        return real_check(url, allow, resolver)

    monkeypatch.setattr(wh.netguard, "check_url", check)

    app = create_app()
    app.state.authorizer = FakeAuthorizer(actor_id=pg["user_id"])
    app.state.audit_runtime = AuditRuntime()
    app.state.webhooks_admin_runtime = WebhookAdminRuntime(
        sessions=pg["sessions"], settings=get_settings(), audit=PgAuditSink()
    )
    async with pg["sessions"]() as s:
        await s.execute(
            update(App).where(App.id == pg["app_a"]).values(webhook_config=_webhook_config())
        )
        await s.commit()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http, calls
    async with pg["sessions"]() as s:
        await s.execute(delete(AuditLog).where(AuditLog.app_id.in_([pg["app_a"], pg["app_b"]])))
        await s.execute(
            update(App).where(App.id.in_([pg["app_a"], pg["app_b"]])).values(webhook_config={})
        )
        await s.commit()


async def _delivery(pg: dict[str, Any], app_id: UUID, *, status: str = "failed") -> UUID:
    async with pg["sessions"]() as s:
        conv = await ConversationRepository(s).create(
            app_id, NewConversation(channel="api", external_user_id=None)
        )
        event = ConversationEvent(
            id=uuid.uuid4(),
            conversation_id=conv.id,
            app_id=app_id,
            seq=1,
            type="message.created",
            data={"message": {"id": str(uuid.uuid4())}},
        )
        s.add(event)
        await s.flush()
        row = WebhookDelivery(
            id=uuid.uuid4(),
            event_id=event.id,
            app_id=app_id,
            conversation_id=conv.id,
            event="message.created",
            seq=1,
            payload={"type": "message.created"},
            status=status,
            attempts=6 if status == "failed" else 1,
            last_status_code=500,
            last_error="HTTP 500",
            next_attempt_at=None,
        )
        s.add(row)
        await s.commit()
    return row.id


async def test_config_hides_ciphertext(ops: Any, pg: dict[str, Any]) -> None:
    http, _calls = ops
    body = (await http.get(f"/api/v1/admin/apps/{pg['app_a']}/webhook", headers=HEADERS)).json()
    assert body["url"] == URL
    assert body["ready"] is True
    assert body["secret_prefix"].startswith("whsec_")
    assert "secret_enc" not in body


async def test_deliveries_filter_and_hide_payload(ops: Any, pg: dict[str, Any]) -> None:
    http, _calls = ops
    failed = await _delivery(pg, pg["app_a"], status="failed")
    await _delivery(pg, pg["app_a"], status="delivered")
    response = await http.get(
        f"/api/v1/admin/apps/{pg['app_a']}/webhook-deliveries",
        headers=HEADERS,
        params={"status": "failed"},
    )
    body = response.json()
    assert [item["id"] for item in body["items"]] == [str(failed)]
    assert "payload" not in body["items"][0]


async def test_redeliver_resets_and_writes_audit(ops: Any, pg: dict[str, Any]) -> None:
    http, _calls = ops
    delivery_id = await _delivery(pg, pg["app_a"], status="failed")
    response = await http.post(
        f"/api/v1/admin/apps/{pg['app_a']}/webhook-deliveries/{delivery_id}/redeliver",
        headers=HEADERS,
    )
    body = response.json()
    assert body["status"] == "pending"
    async with pg["sessions"]() as s:
        row = await s.get(WebhookDelivery, delivery_id)
        assert row is not None and row.status == "pending" and row.attempts == 0
        audit = await s.scalar(select(AuditLog).where(AuditLog.action == "webhook.redeliver"))
        assert audit is not None and str(audit.target_id) == str(delivery_id)


async def test_redeliver_pending_row_is_404(ops: Any, pg: dict[str, Any]) -> None:
    http, _calls = ops
    delivery_id = await _delivery(pg, pg["app_a"], status="pending")
    response = await http.post(
        f"/api/v1/admin/apps/{pg['app_a']}/webhook-deliveries/{delivery_id}/redeliver",
        headers=HEADERS,
    )
    assert response.status_code == 404
    assert response.json()["error"]["details"]["delivery_id"] == str(delivery_id)


async def test_send_test_returns_result_and_does_not_persist(ops: Any, pg: dict[str, Any]) -> None:
    http, calls = ops
    async with pg["sessions"]() as s:
        before = len(
            (
                await s.scalars(
                    select(WebhookDelivery).where(WebhookDelivery.app_id == pg["app_a"])
                )
            ).all()
        )
    body = (
        await http.post(f"/api/v1/admin/apps/{pg['app_a']}/webhook/test", headers=HEADERS)
    ).json()
    assert body["ok"] is True and body["status_code"] == 204
    assert calls and b'"ping"' in calls[-1]["body"]
    async with pg["sessions"]() as s:
        after = len(
            (
                await s.scalars(
                    select(WebhookDelivery).where(WebhookDelivery.app_id == pg["app_a"])
                )
            ).all()
        )
        audit = await s.scalar(select(AuditLog).where(AuditLog.action == "webhook.test"))
    assert after == before
    assert audit is not None


async def test_rotate_secret_returns_plaintext_once(ops: Any, pg: dict[str, Any]) -> None:
    http, _calls = ops
    body = (
        await http.post(f"/api/v1/admin/apps/{pg['app_a']}/webhook/secret", headers=HEADERS)
    ).json()
    assert body["secret"].startswith("whsec_")
    assert body["secret_prefix"] == body["secret"][: len(body["secret_prefix"])]
    async with pg["sessions"]() as s:
        app_row = await s.get(App, pg["app_a"])
        assert app_row is not None
        key = get_settings().data_encryption_key.get_secret_value()
        assert secretbox.decrypt(app_row.webhook_config["secret_enc"], key) == body["secret"]
        assert app_row.webhook_config["url"] == URL


async def test_cross_app_delivery_is_404(ops: Any, pg: dict[str, Any]) -> None:
    http, _calls = ops
    delivery_id = await _delivery(pg, pg["app_a"], status="failed")
    response = await http.post(
        f"/api/v1/admin/apps/{pg['app_b']}/webhook-deliveries/{delivery_id}/redeliver",
        headers=HEADERS,
    )
    assert response.status_code == 404
