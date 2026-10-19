"""出站 Webhook 的签名与请求头（第三方接入指南 §7）。"""

from __future__ import annotations

import hashlib
import hmac
from uuid import UUID

#: 事件名（`conversation.closed` 之类）。
EVENT_HEADER = "X-AICS-Event"
#: 投递 ID（= 事件 ID）；接收方按它去重。
DELIVERY_HEADER = "X-AICS-Delivery"
#: Unix 秒；接收方拒绝与本机时间相差超过 5 分钟的请求（防重放）。
TIMESTAMP_HEADER = "X-AICS-Timestamp"
#: `sha256=<hex>`。
SIGNATURE_HEADER = "X-AICS-Signature"
#: 固定 UA，方便接收方做访问日志与限流。
USER_AGENT = "aics-webhook/1"


def sign(secret: str, timestamp: int, body: bytes) -> str:
    """`HMAC-SHA256(secret, "{timestamp}.{body}")`，前缀 `sha256=`。"""
    message = f"{timestamp}.".encode() + body
    digest = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def headers(
    *, delivery_id: UUID | str, event: str, timestamp: int, signature: str
) -> dict[str, str]:
    """一次投递的全部请求头（含 `Content-Type`）。"""
    return {
        EVENT_HEADER: event,
        DELIVERY_HEADER: str(delivery_id),
        TIMESTAMP_HEADER: str(timestamp),
        SIGNATURE_HEADER: signature,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
