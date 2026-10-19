"""M3.5 ③：Webhook 密钥、签名与出站投递（不需要数据库）。"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from aics.core import netguard, secretbox
from aics.core.config import get_settings
from aics.modules.apps.service import AppService
from aics.modules.webhooks import service as wh
from aics.modules.webhooks import signing

NOW = datetime.now(UTC)


@pytest.fixture(autouse=True)
def _allow_urls(monkeypatch: Any) -> None:
    """默认让地址校验通过：本机 DNS 会把 example.com 解析到保留网段（198.18.x.x）。

    需要「被拒」的用例自己再 patch 一次，覆盖这个桩。
    """
    monkeypatch.setattr(
        wh.netguard, "check_url", lambda url, allow, resolver=None: ["93.184.216.34"]
    )


def _settings(**kw: Any) -> Any:
    base = {"data_encryption_key": SecretStr("unit-test-master-key"), "url_fetch_allowlist": []}
    base.update(kw)
    return get_settings().model_copy(update=base)


# ---------------------------------------------------------------- 密钥与签名
def test_secretbox_roundtrip_and_prefix() -> None:
    plain = secretbox.new_secret()
    assert plain.startswith(secretbox.SECRET_PREFIX)
    assert secretbox.prefix_of(plain) == plain[: secretbox.PREFIX_LENGTH]
    token = secretbox.encrypt(plain, "master-key")
    assert plain not in token
    assert secretbox.decrypt(token, "master-key") == plain


def test_secretbox_accepts_a_standard_fernet_key() -> None:
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    assert secretbox.decrypt(secretbox.encrypt("whsec_x", key), key) == "whsec_x"


def test_secretbox_rejects_a_foreign_key() -> None:
    token = secretbox.encrypt("whsec_x", "master-key")
    with pytest.raises(secretbox.InvalidToken):
        secretbox.decrypt(token, "another-key")


def test_signature_matches_manual_hmac() -> None:
    body = b'{"id":"x"}'
    expected = hmac.new(b"whsec_s", b"1700000000." + body, hashlib.sha256).hexdigest()
    assert signing.sign("whsec_s", 1700000000, body) == f"sha256={expected}"


def test_signed_headers_cover_the_contract() -> None:
    headers = signing.headers(
        delivery_id=UUID("0198f4b0-0000-7000-8000-000000000001"),
        event="handoff.requested",
        timestamp=1700000000,
        signature="sha256=abc",
    )
    assert headers[signing.EVENT_HEADER] == "handoff.requested"
    assert headers[signing.SIGNATURE_HEADER] == "sha256=abc"
    assert headers[signing.TIMESTAMP_HEADER] == "1700000000"
    assert headers["Content-Type"] == "application/json"
    assert headers["User-Agent"] == signing.USER_AGENT


# ---------------------------------------------------------------- 投递
class _Rows:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar_one_or_none(self) -> Any:
        return self.value


class FakeSession:
    def __init__(self, webhook_config: dict[str, Any] | None = None) -> None:
        self.webhook_config = webhook_config
        self.commits = 0

    async def execute(self, stmt: Any) -> _Rows:
        return _Rows(
            None
            if self.webhook_config is None
            else SimpleNamespace(webhook_config=self.webhook_config)
        )

    async def commit(self) -> None:
        self.commits += 1


class FakeRepo:
    def __init__(self, row: Any, *, due: list[UUID] | None = None, changed: int = 1) -> None:
        self.row = row
        self.due_ids = due or []
        self.changed = changed
        self.calls: list[tuple[str, Any]] = []

    async def load_all_apps(self, delivery_id: UUID) -> Any:
        return self.row

    async def mark_delivered(self, app_id: UUID, id: UUID, *, at: datetime) -> int:
        self.calls.append(("delivered", {"at": at}))
        return 1

    async def mark_attempt(self, app_id: UUID, id: UUID, **kw: Any) -> int:
        self.calls.append(("attempt", kw))
        return 1

    async def due_all_apps(self, *, before: datetime, limit: int) -> list[UUID]:
        return list(self.due_ids)

    async def claim_all_apps(self, id: UUID, *, at: datetime) -> int:
        self.calls.append(("claim", {"id": id, "at": at}))
        return 1

    async def reset_for_redelivery(self, app_id: UUID, id: UUID, *, at: datetime) -> int:
        self.calls.append(("reset", {"at": at}))
        return self.changed


def _row(*, status: str = "pending", attempts: int = 0) -> Any:
    return SimpleNamespace(
        id=uuid4(),
        app_id=uuid4(),
        status=status,
        attempts=attempts,
        payload={"id": "evt", "seq": 1},
        event="handoff.requested",
    )


def _configured(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    secret = secretbox.new_secret()
    return {
        "url": "https://example.com/hook",
        "secret_enc": secretbox.encrypt(secret, "unit-test-master-key"),
        "secret_prefix": secretbox.prefix_of(secret),
        **(cfg or {}),
    }


async def _deliver(
    sender: Any, *, row: Any = None, webhook: dict[str, Any] | None = None
) -> tuple[wh.DeliveryResult, FakeRepo, FakeSession]:
    row = row or _row()
    repo = FakeRepo(row)
    session = FakeSession(_configured(webhook))
    svc = wh.WebhookService(
        session,
        settings=_settings(),
        repo=repo,
        sender=sender,  # type: ignore[arg-type]
    )
    return await svc.deliver(row.id), repo, session


async def test_deliver_marks_delivered_on_2xx() -> None:
    sent: list[dict[str, str]] = []

    async def sender(url: str, body: bytes, headers: dict[str, str]) -> int:
        sent.append(headers)
        assert body == b'{"id":"evt","seq":1}'
        return 204

    result, repo, session = await _deliver(sender)
    assert result.status == "delivered" and result.status_code == 204
    assert [c[0] for c in repo.calls] == ["delivered"]
    assert session.commits == 1
    assert sent[0][signing.SIGNATURE_HEADER].startswith("sha256=")


async def test_deliver_schedules_retry_on_non_2xx() -> None:
    async def sender(url: str, body: bytes, headers: dict[str, str]) -> int:
        return 500

    result, repo, _ = await _deliver(sender)
    assert result.status == "retry" and result.attempts == 1
    kind, values = repo.calls[0]
    assert kind == "attempt" and values["failed"] is False and values["status_code"] == 500
    gap = values["next_attempt_at"] - datetime.now(UTC)
    assert timedelta(seconds=9) < gap < timedelta(seconds=11)  # 第 1 次失败等 10 秒


async def test_deliver_gives_up_on_the_sixth_attempt() -> None:
    async def sender(url: str, body: bytes, headers: dict[str, str]) -> int:
        return 503

    result, repo, _ = await _deliver(sender, row=_row(attempts=wh.MAX_ATTEMPTS - 1))
    assert result.status == "failed" and result.next_attempt_at is None
    kind, values = repo.calls[0]
    assert kind == "attempt" and values["failed"] is True and values["next_attempt_at"] is None


async def test_deliver_treats_exceptions_as_retryable() -> None:
    async def sender(url: str, body: bytes, headers: dict[str, str]) -> int:
        raise TimeoutError("boom")

    result, repo, _ = await _deliver(sender)
    assert result.status == "retry" and result.error == "TimeoutError"
    assert repo.calls[0][1]["status_code"] is None


async def test_deliver_skips_missing_and_finished_rows() -> None:
    repo = FakeRepo(None)
    svc = wh.WebhookService(
        FakeSession(_configured()),
        settings=_settings(),
        repo=repo,  # type: ignore[arg-type]
    )
    assert (await svc.deliver(uuid4())).status == "missing"

    finished = _row(status="delivered", attempts=2)
    repo = FakeRepo(finished)
    svc = wh.WebhookService(
        FakeSession(_configured()),
        settings=_settings(),
        repo=repo,  # type: ignore[arg-type]
    )
    result = await svc.deliver(finished.id)
    assert result.status == "skipped" and result.attempts == 2
    assert repo.calls == []


async def test_deliver_gives_up_without_a_target() -> None:
    async def sender(url: str, body: bytes, headers: dict[str, str]) -> int:  # pragma: no cover
        raise AssertionError("不该发出去")

    row = _row()
    repo = FakeRepo(row)
    svc = wh.WebhookService(
        FakeSession(None),
        settings=_settings(),
        repo=repo,
        sender=sender,  # type: ignore[arg-type]
    )
    result = await svc.deliver(row.id)
    assert result.status == "failed" and "尚未配置" in (result.error or "")
    assert repo.calls[0][1]["failed"] is True


async def test_deliver_gives_up_when_the_secret_cannot_be_decrypted() -> None:
    async def sender(url: str, body: bytes, headers: dict[str, str]) -> int:  # pragma: no cover
        raise AssertionError("不该发出去")

    row = _row()
    repo = FakeRepo(row)
    svc = wh.WebhookService(
        FakeSession({"url": "https://example.com/hook", "secret_enc": "garbage"}),
        settings=_settings(),
        repo=repo,  # type: ignore[arg-type]
        sender=sender,
    )
    result = await svc.deliver(row.id)
    assert result.status == "failed" and result.error == "secret 无法解密"


async def test_deliver_gives_up_when_the_url_is_blocked(monkeypatch: Any) -> None:
    async def sender(url: str, body: bytes, headers: dict[str, str]) -> int:  # pragma: no cover
        raise AssertionError("不该发出去")

    def blocked(url: str, allow: Any, resolver: Any = None) -> list[str]:
        raise netguard.UrlNotAllowedError()

    monkeypatch.setattr(wh.netguard, "check_url", blocked)
    result, repo, _ = await _deliver(sender)
    assert result.status == "failed"
    assert repo.calls[0][1]["failed"] is True


async def test_due_claims_each_row_for_a_minute() -> None:
    first, second = uuid4(), uuid4()
    repo = FakeRepo(_row(), due=[first, second])
    session = FakeSession(None)
    svc = wh.WebhookService(session, settings=_settings(), repo=repo)  # type: ignore[arg-type]
    ids = await svc.due(now=NOW)
    assert ids == [first, second]
    claims = [c[1] for c in repo.calls]
    assert [c["id"] for c in claims] == [first, second]
    assert claims[0]["at"] == NOW + timedelta(seconds=wh.RETRY_CLAIM_S)
    assert session.commits == 1


async def test_due_without_rows_does_not_commit() -> None:
    repo = FakeRepo(_row())
    session = FakeSession(None)
    svc = wh.WebhookService(session, settings=_settings(), repo=repo)  # type: ignore[arg-type]
    assert await svc.due(now=NOW) == []
    assert session.commits == 0


async def test_redeliver_returns_whether_a_row_changed() -> None:
    session = FakeSession(None)
    svc = wh.WebhookService(
        session,
        settings=_settings(),
        repo=FakeRepo(_row(), changed=0),  # type: ignore[arg-type]
    )
    assert await svc.redeliver(uuid4(), uuid4()) is False
    svc = wh.WebhookService(
        session,
        settings=_settings(),
        repo=FakeRepo(_row(), changed=1),  # type: ignore[arg-type]
    )
    assert await svc.redeliver(uuid4(), uuid4()) is True


async def test_send_test_does_not_persist_anything() -> None:
    bodies: list[dict[str, Any]] = []

    async def sender(url: str, body: bytes, headers: dict[str, str]) -> int:
        import json

        bodies.append(json.loads(body))
        assert headers[signing.EVENT_HEADER] == "ping"
        return 200

    repo = FakeRepo(_row())
    svc = wh.WebhookService(
        FakeSession(_configured()),
        settings=_settings(),
        repo=repo,
        sender=sender,  # type: ignore[arg-type]
    )
    result = await svc.send_test(uuid4())
    assert result.ok is True and result.status_code == 200
    assert bodies[0]["event"] == "ping" and bodies[0]["seq"] == 0
    assert repo.calls == []


async def test_send_test_requires_a_configured_target() -> None:
    svc = wh.WebhookService(FakeSession(None), settings=_settings(), repo=FakeRepo(_row()))  # type: ignore[arg-type]
    with pytest.raises(netguard.UrlNotAllowedError):
        await svc.send_test(uuid4())


# ---------------------------------------------------------------- 保存期校验与密钥轮换
class _AppRepo:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.saved: list[dict[str, Any]] = []

    async def get_app(self, app_id: UUID) -> Any:
        return self.app

    async def save_values(self, app_id: UUID, values: dict[str, Any]) -> None:
        self.saved.append(values)


def _app(**webhook: Any) -> Any:
    return SimpleNamespace(
        id=uuid4(),
        name="demo",
        status="active",
        config_version=1,
        chunk_config_version=1,
        rag_config={},
        handoff_config={},
        session_config={},
        chunk_config={},
        webhook_config=webhook,
        custom_dict=[],
    )


def _app_service(app: Any) -> tuple[AppService, _AppRepo]:
    svc = AppService(cast_session(), settings=_settings())
    repo = _AppRepo(app)
    svc.repo = repo  # type: ignore[assignment]
    return svc, repo


class _Session:
    async def refresh(self, obj: Any) -> None:
        return None

    async def commit(self) -> None:
        return None


def cast_session() -> AsyncSession:
    return _Session()  # type: ignore[return-value]


async def test_rotate_webhook_secret_stores_only_ciphertext() -> None:
    svc, repo = _app_service(_app(url="https://example.com/hook"))
    plain = await svc.rotate_webhook_secret(UUID(int=1))
    assert plain.startswith(secretbox.SECRET_PREFIX)
    stored = repo.saved[0]["webhook_config"]
    assert stored["secret_prefix"] == secretbox.prefix_of(plain)
    assert plain not in str(stored)
    assert secretbox.decrypt(stored["secret_enc"], "unit-test-master-key") == plain


async def test_update_app_rejects_a_private_webhook_url(monkeypatch: Any) -> None:
    def blocked(url: str, allow: Any, resolver: Any = None) -> list[str]:
        raise netguard.UrlNotAllowedError()

    monkeypatch.setattr("aics.modules.apps.service.netguard.check_url", blocked)
    svc, repo = _app_service(_app())
    with pytest.raises(netguard.UrlNotAllowedError):
        await svc.update_app(UUID(int=1), {"webhook_config": {"url": "http://127.0.0.1:8080/x"}})
    assert repo.saved == []


async def test_update_app_accepts_a_public_webhook_url(monkeypatch: Any) -> None:
    seen: list[str] = []

    def allowed(url: str, allow: Any, resolver: Any = None) -> list[str]:
        seen.append(url)
        return ["93.184.216.34"]

    monkeypatch.setattr("aics.modules.apps.service.netguard.check_url", allowed)
    svc, repo = _app_service(_app())
    await svc.update_app(UUID(int=1), {"webhook_config": {"url": "https://example.com/hook"}})
    assert seen == ["https://example.com/hook"]
    assert repo.saved[0]["webhook_config"]["url"] == "https://example.com/hook"


async def test_update_app_does_not_validate_when_url_is_unchanged(monkeypatch: Any) -> None:
    def boom(url: str, allow: Any, resolver: Any = None) -> list[str]:  # pragma: no cover
        raise AssertionError("没改地址就不该校验")

    monkeypatch.setattr("aics.modules.apps.service.netguard.check_url", boom)
    svc, _ = _app_service(_app())
    await svc.update_app(UUID(int=1), {"name": "renamed"})
