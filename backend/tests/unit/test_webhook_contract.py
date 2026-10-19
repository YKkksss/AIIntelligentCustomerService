"""M3.5 ④ 契约：签名与第三方接入指南 §7.4 的示例互通，八种事件的 payload 快照。

这些用例是「文档 ↔ 实现」的锚点：接入方照文档抄的验签代码必须能验我们的签名，
payload 的字段名与形状一旦变动就要在这里显式改一次（而不是悄悄漂移）。
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from aics.modules.webhooks import payloads, signing
from aics.modules.webhooks import service as wh
from tests.webhook_support import verify

SECRET = "whsec_2f8c1f6a9d4b4f0e8c3a5b7d1e2f3a4b"
BODY = b'{"event":"handoff.requested"}'
APP = uuid.UUID("0198f0aa-0000-7000-8000-0000000000aa")
CONV = uuid.UUID("0198f0aa-0000-7000-8000-0000000000cc")
DELIVERY = uuid.UUID("0198f0aa-0000-7000-8000-0000000000dd")
OCCURRED = datetime(2026, 10, 19, 4, 30, tzinfo=UTC)
ISO = "2026-10-19T04:30:00+00:00"

MESSAGE = SimpleNamespace(
    id=uuid.UUID("0198f0aa-0000-7000-8000-000000000001"),
    role="system",
    kind="handoff_queue_timeout",
    content="当前客服繁忙，您可以留下问题和联系方式，我们会尽快回复您",
    agent_id=None,
    answer_type=None,
    created_at=OCCURRED,
)
EXPECTED_MESSAGE: dict[str, Any] = {
    "id": "0198f0aa-0000-7000-8000-000000000001",
    "role": "system",
    "kind": "handoff_queue_timeout",
    "content": "当前客服繁忙，您可以留下问题和联系方式，我们会尽快回复您",
    "agent_id": None,
    "answer_type": None,
    "created_at": ISO,
}


# --------------------------------------------------------------------------- 签名 ↔ 文档示例


def test_documented_verify_accepts_our_signature() -> None:
    """§7.4 的示例代码必须能验过 `signing.sign` 的产物（含 `sha256=` 前缀）。"""
    ts = int(datetime.now(UTC).timestamp())
    sig = signing.sign(SECRET, ts, BODY)
    assert sig.startswith("sha256=") and len(sig) == len("sha256=") + 64
    assert verify(SECRET, str(ts), BODY, sig) is True


@pytest.mark.parametrize(
    ("secret", "offset", "body", "sig_of"),
    [
        ("whsec_其它密钥", 0, BODY, "self"),  # 密钥不对
        (SECRET, -301, BODY, "self"),  # 时间戳超出 5 分钟窗口
        (SECRET, 0, b'{"event":"tampered"}', "self"),  # 正文被改
        (SECRET, 0, BODY, "other"),  # 签名本身不匹配
    ],
)
def test_documented_verify_rejects(secret: str, offset: int, body: bytes, sig_of: str) -> None:
    ts = int(datetime.now(UTC).timestamp()) + offset
    other = signing.sign("whsec_another", ts, body) if sig_of == "other" else None
    sig = other or signing.sign(SECRET, ts, BODY)
    assert verify(secret, str(ts), body, sig) is False


def test_headers_carry_what_the_receiver_reads() -> None:
    """接收方按这四个头取值（§7.2）：事件名、投递 id、时间戳、签名。"""
    ts = int(OCCURRED.timestamp())
    headers = signing.headers(
        delivery_id=str(DELIVERY),
        event="handoff.requested",
        timestamp=ts,
        signature=signing.sign(SECRET, ts, BODY),
    )
    assert headers[signing.EVENT_HEADER] == "handoff.requested"
    assert headers[signing.DELIVERY_HEADER] == str(DELIVERY)
    assert headers[signing.TIMESTAMP_HEADER] == str(ts)
    assert headers[signing.SIGNATURE_HEADER].startswith("sha256=")
    assert headers["Content-Type"] == "application/json"
    assert headers["User-Agent"] == signing.USER_AGENT


# --------------------------------------------------------------------------- payload 快照


def test_envelope_snapshot() -> None:
    """公共字段（§7.2）：八个键、顺序固定、时间统一 UTC。"""
    payload = payloads.build_payload(
        delivery_id=DELIVERY,
        event="handoff.requested",
        app_id=APP,
        conversation_id=CONV,
        user_id="u_10086",
        seq=7,
        occurred_at=OCCURRED,
        data={"reason": "keyword"},
    )
    assert payload == {
        "id": str(DELIVERY),
        "event": "handoff.requested",
        "app_id": str(APP),
        "conversation_id": str(CONV),
        "user_id": "u_10086",
        "seq": 7,
        "occurred_at": ISO,
        "data": {"reason": "keyword"},
    }
    assert list(payload) == [
        "id",
        "event",
        "app_id",
        "conversation_id",
        "user_id",
        "seq",
        "occurred_at",
        "data",
    ]


def test_envelope_allows_no_conversation() -> None:
    """无状态会话（`/v1` 不落会话）时 `conversation_id` 为 null。"""
    payload = payloads.build_payload(
        delivery_id=DELIVERY,
        event="message.created",
        app_id=APP,
        conversation_id=None,
        user_id=None,
        seq=0,
        occurred_at=OCCURRED,
        data={},
    )
    assert payload["conversation_id"] is None and payload["user_id"] is None


CASES: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
    (
        "message.created",
        payloads.message_created(MESSAGE),
        {"message": EXPECTED_MESSAGE},
    ),
    (
        "handoff.requested",
        payloads.handoff_requested(
            reason="keyword",
            intent="handoff",
            handoff_ai_mode="wait",
            summary="用户要转人工",
            facts={"order_no": "A1"},
            recent=[MESSAGE],
        ),
        {
            "reason": "keyword",
            "intent": "handoff",
            "handoff_ai_mode": "wait",
            "summary": "用户要转人工",
            "facts": {"order_no": "A1"},
            "recent_messages": [
                {
                    "id": EXPECTED_MESSAGE["id"],
                    "role": "system",
                    "kind": "handoff_queue_timeout",
                    "content": EXPECTED_MESSAGE["content"],
                    "created_at": ISO,
                }
            ],
        },
    ),
    (
        "handoff.offline",
        payloads.handoff_offline(
            message=MESSAGE,
            reason="keyword",
            needs_callback=True,
            summary=None,
            facts=None,
            recent=[MESSAGE],
        ),
        {
            "message": EXPECTED_MESSAGE,
            "reason": "keyword",
            "needs_callback": True,
            "summary": None,
            "facts": {},
            "recent_messages": [
                {
                    "id": EXPECTED_MESSAGE["id"],
                    "role": "system",
                    "kind": "handoff_queue_timeout",
                    "content": EXPECTED_MESSAGE["content"],
                    "created_at": ISO,
                }
            ],
        },
    ),
    (
        "handoff.accepted",
        payloads.handoff_accepted(agent_id="ag_1", agent_name="小王"),
        {"agent_id": "ag_1", "agent_name": "小王"},
    ),
    (
        "handoff.released",
        payloads.handoff_released(
            agent_id="ag_1", note="已解决", released_by="agent", reason="agent"
        ),
        {"agent_id": "ag_1", "note": "已解决", "released_by": "agent", "reason": "agent"},
    ),
    (
        "handoff.queue_timeout",
        payloads.handoff_queue_timeout(message=MESSAGE, waited_seconds=360, needs_callback=True),
        {"message": EXPECTED_MESSAGE, "waited_seconds": 360, "needs_callback": True},
    ),
    (
        "conversation.idle_notice",
        payloads.idle_notice(message=MESSAGE, close_at=OCCURRED),
        {"message": EXPECTED_MESSAGE, "close_at": ISO},
    ),
    (
        "conversation.closed",
        payloads.conversation_closed(reason="handoff_idle"),
        {"reason": "handoff_idle"},
    ),
]


@pytest.mark.parametrize(("event", "data", "expected"), CASES, ids=[c[0] for c in CASES])
def test_data_snapshot(event: str, data: dict[str, Any], expected: dict[str, Any]) -> None:
    """八种事件的 `data` 快照（§7.3）——事件名与 data 一一对应，不允许悄悄加字段。"""
    assert data == expected
    payload = payloads.build_payload(
        delivery_id=DELIVERY,
        event=event,
        app_id=APP,
        conversation_id=CONV,
        user_id="u_10086",
        seq=1,
        occurred_at=OCCURRED,
        data=data,
    )
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    assert json.loads(body) == payload and "\\u" not in body.decode()  # 中文不转义


def test_retry_and_timeout_numbers_match_the_guide() -> None:
    """第三方接入指南 §7.2 写死的投递参数：5 秒超时、退避表、单批 100。"""
    assert wh.TIMEOUT_S == 5.0
    assert wh.RETRY_DELAYS_S == (10, 30, 120, 600, 1800)
    assert len(wh.RETRY_DELAYS_S) + 1 == wh.MAX_ATTEMPTS  # 首次 + 5 次重试 = 6 次
    assert wh.RETRY_BATCH == 100


def test_summary_text_renders_the_three_labels() -> None:
    """滚动摘要的渲染口径（§7.3）：目标 + 已确认 / 已给出 / 待解决。"""
    assert payloads.summary_text(None) is None
    assert payloads.summary_text({}) is None
    rendered = payloads.summary_text(
        {
            "user_goal": "退货",
            "confirmed_info": ["订单号 A1", ""],
            "solutions_given": ["已告知 7 天无理由"],
            "open_issues": ["等待上门取件"],
        }
    )
    assert rendered == "退货；已确认：订单号 A1；已给出：已告知 7 天无理由；待解决：等待上门取件"
