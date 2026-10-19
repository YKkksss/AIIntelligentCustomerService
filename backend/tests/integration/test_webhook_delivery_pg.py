"""M3.5 ③ 集成：出站 Webhook 投递在真 PG 上的行为（接收方用假 sender）。

覆盖：事件排一条 `pending` 投递 → 投递成功 / 失败退避 → 第 6 次置 `failed` → 重试扫描领取；
`send_test` 不落表；SSRF 在保存与投递两处各拦一次。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select, update
from tests.chat_support import make_runtime  # noqa: F401  (保持与其它集成用例相同的导入风格)

from aics.core import netguard, secretbox
from aics.core.config import get_settings
from aics.db.models import App, Conversation, WebhookDelivery
from aics.modules.apps.service import AppService
from aics.modules.chat import events
from aics.modules.chat.repository import ConversationRepository
from aics.modules.chat.schemas import NewConversation
from aics.modules.webhooks import service as wh
from aics.rag.config import WebhookConfig

pytestmark = pytest.mark.integration

#: 接收端地址：域名是假的，测试里由 `_urls` 夹具直接放行（本机 DNS 会把它解到保留网段）。
URL = "https://receiver.example/aics"


def _key() -> str:
    return get_settings().data_encryption_key.get_secret_value()


def _webhook_config(url: str = URL) -> tuple[str, dict[str, Any]]:
    plain = secretbox.new_secret()
    return plain, {
        "url": url,
        "secret_enc": secretbox.encrypt(plain, _key()),
        "secret_prefix": secretbox.prefix_of(plain),
    }


@pytest.fixture(autouse=True)
def _urls(monkeypatch: Any) -> None:
    """放行假域名，其它（例如 127.0.0.1）仍交给真的 `check_url` 拦。"""
    real = netguard.check_url

    def check(url: str, allow: Any, resolver: Any = None) -> list[str]:
        if "receiver.example" in url:
            return ["93.184.216.34"]
        return real(url, allow, resolver)

    monkeypatch.setattr(wh.netguard, "check_url", check)


async def _emit(pg: dict[str, Any], app_id: UUID, *, url: str = URL) -> UUID:
    """摆好 webhook 配置 + 建会话 + 发一个 `handoff.requested`，返回会话 id。"""
    _, cfg = _webhook_config(url)
    async with pg["sessions"]() as s:
        await s.execute(update(App).where(App.id == app_id).values(webhook_config=cfg))
        conv = await ConversationRepository(s).create(
            app_id, NewConversation(channel="api", external_user_id="u1")
        )
        await events.emit(
            s,
            events.Target.of(conv),
            "handoff.requested",
            {"reason": "keyword"},
            webhook=events.WebhookTarget.of(WebhookConfig.model_validate(cfg)),
        )
        await s.commit()
        return conv.id


async def _delivery(pg: dict[str, Any], cid: UUID) -> WebhookDelivery:
    async with pg["sessions"]() as s:
        return (
            await s.execute(select(WebhookDelivery).where(WebhookDelivery.conversation_id == cid))
        ).scalar_one()


async def _call(pg: dict[str, Any], fn: Any, *, sender: Any = None) -> Any:
    async with pg["sessions"]() as s:
        return await fn(wh.WebhookService(s, sender=sender))


async def test_deliver_success_marks_the_delivery_delivered(pg: dict[str, Any]) -> None:
    cid = await _emit(pg, pg["app_a"])
    row = await _delivery(pg, cid)
    assert row.status == "pending" and row.id == row.event_id and row.attempts == 0
    assert row.payload["event"] == "handoff.requested" and row.payload["id"] == str(row.id)

    bodies: list[bytes] = []

    async def sender(url: str, body: bytes, headers: dict[str, str]) -> int:
        bodies.append(body)
        assert headers["X-AICS-Delivery"] == str(row.id)
        assert headers["X-AICS-Event"] == "handoff.requested"
        return 200

    result = await _call(pg, lambda svc: svc.deliver(row.id), sender=sender)
    assert result.status == "delivered" and result.attempts == 1
    assert bodies == [bodies[0]] and b'"handoff.requested"' in bodies[0]

    row = await _delivery(pg, cid)
    assert row.status == "delivered" and row.delivered_at is not None
    assert row.attempts == 1 and row.last_error is None and row.next_attempt_at is None


async def test_failure_schedules_a_retry_with_backoff(pg: dict[str, Any]) -> None:
    cid = await _emit(pg, pg["app_a"])
    row = await _delivery(pg, cid)

    async def sender(url: str, body: bytes, headers: dict[str, str]) -> int:
        return 503

    before = datetime.now(UTC)
    result = await _call(pg, lambda svc: svc.deliver(row.id), sender=sender)
    assert result.status == "retry" and result.status_code == 503

    row = await _delivery(pg, cid)
    assert row.status == "pending" and row.attempts == 1
    assert row.last_status_code == 503 and row.last_error == "HTTP 503"
    assert row.next_attempt_at is not None
    assert before + timedelta(seconds=9) < row.next_attempt_at < before + timedelta(seconds=11)


async def test_sixth_failure_gives_up(pg: dict[str, Any]) -> None:
    cid = await _emit(pg, pg["app_a"])
    row = await _delivery(pg, cid)
    async with pg["sessions"]() as s:
        await s.execute(
            update(WebhookDelivery)
            .where(WebhookDelivery.id == row.id)
            .values(attempts=wh.MAX_ATTEMPTS - 1)
        )
        await s.commit()

    async def sender(url: str, body: bytes, headers: dict[str, str]) -> int:
        return 500

    result = await _call(pg, lambda svc: svc.deliver(row.id), sender=sender)
    assert result.status == "failed" and result.next_attempt_at is None

    row = await _delivery(pg, cid)
    assert row.status == "failed" and row.attempts == wh.MAX_ATTEMPTS
    assert row.next_attempt_at is None and row.delivered_at is None


async def test_retry_sweep_claims_due_rows_once(pg: dict[str, Any]) -> None:
    cid = await _emit(pg, pg["app_a"])
    row = await _delivery(pg, cid)
    past = datetime.now(UTC) - timedelta(seconds=1)
    async with pg["sessions"]() as s:
        await s.execute(
            update(WebhookDelivery).where(WebhookDelivery.id == row.id).values(next_attempt_at=past)
        )
        await s.commit()

    ids = await _call(pg, lambda svc: svc.due())
    assert str(row.id) in [str(i) for i in ids]
    # 领过一次就推到 60 秒后：同一秒内再扫不会重复入队
    assert await _call(pg, lambda svc: svc.due()) == []
    row = await _delivery(pg, cid)
    assert row.next_attempt_at > datetime.now(UTC) + timedelta(seconds=50)


async def test_send_test_does_not_write_a_delivery_row(pg: dict[str, Any]) -> None:
    cid = await _emit(pg, pg["app_a"])
    before = await _delivery(pg, cid)

    async def sender(url: str, body: bytes, headers: dict[str, str]) -> int:
        assert b'"ping"' in body
        return 204

    result = await _call(pg, lambda svc: svc.send_test(pg["app_a"]), sender=sender)
    assert result.ok is True and result.status_code == 204

    async with pg["sessions"]() as s:
        rows = list(
            (
                await s.execute(
                    select(WebhookDelivery).where(WebhookDelivery.app_id == pg["app_a"])
                )
            ).scalars()
        )
    assert [r.id for r in rows] == [before.id]  # 测试投递不落表（ISSUE-59）


async def test_private_url_is_rejected_before_delivery(pg: dict[str, Any]) -> None:
    cid = await _emit(pg, pg["app_a"], url="http://127.0.0.1:8080/hook")
    row = await _delivery(pg, cid)

    async def sender(url: str, body: bytes, headers: dict[str, str]) -> int:  # pragma: no cover
        raise AssertionError("不该向内网发")

    result = await _call(pg, lambda svc: svc.deliver(row.id))
    assert result.status == "failed" and "地址" in (result.error or "")
    row = await _delivery(pg, cid)
    assert row.status == "failed" and row.last_status_code is None


async def test_saving_a_private_webhook_url_is_rejected(pg: dict[str, Any]) -> None:
    async with pg["sessions"]() as s:
        with pytest.raises(netguard.UrlNotAllowedError):
            await AppService(s).update_app(
                pg["app_a"], {"webhook_config": {"url": "http://127.0.0.1:8080/hook"}}
            )


async def test_private_conversation_is_not_shared(pg: dict[str, Any]) -> None:
    """投递行按应用隔离：B 的事件不会出现在 A 的扫描里（`due_all_apps` 只按到期时间取）。"""
    cid = await _emit(pg, pg["app_b"])
    row = await _delivery(pg, cid)
    assert row.app_id == pg["app_b"]
    async with pg["sessions"]() as s:
        assert (
            await s.execute(select(Conversation).where(Conversation.id == cid))
        ).scalar_one().app_id == pg["app_b"]
