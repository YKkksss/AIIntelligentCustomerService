"""M3.5 ⑤ 示例接收端：第三方接入指南 §7.2–7.4 的「接收方」做成一个真能跑的小服务。

里程碑验收脚本（`e2e_m35_webhooks.py`）在本进程里用 uvicorn 起同一个 `app`，所以这个文件
既是给人看的示例，也是验收链路里真的那一端。单独跑也可以：

    cd backend
    AICS_E2E_WEBHOOK_SECRET=whsec_xxx AICS_E2E_WEBHOOK_PORT=9100 \\
        PYTHONPATH=. uv run python -m scripts.e2e_m3.webhook_receiver

行为：
- `POST /hook`：按 §7.4 的写法验签——通过 200，不通过 401；
  `AICS_E2E_WEBHOOK_FAIL_FIRST=N` 时前 N 次**验签通过但返回 500**（用来演示退避重试）。
- `GET /received`：返回收到的投递（脚本断言用）；`POST /reset`：清空记录与失败计数。
- `GET /healthz`：就绪探针。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

#: §7.2 的四个头
EVENT_HEADER = "X-AICS-Event"
DELIVERY_HEADER = "X-AICS-Delivery"
TIMESTAMP_HEADER = "X-AICS-Timestamp"
SIGNATURE_HEADER = "X-AICS-Signature"
#: 接收方自己的防重放窗口（§7.2：拒绝时间戳相差超过 5 分钟的请求）
WINDOW_S = 300


def verify(secret: str, ts: str, body: bytes, sig: str) -> bool:
    """第三方接入指南 §7.4 的示例代码（原样）。"""
    if abs(time.time() - int(ts)) > WINDOW_S:
        return False
    mac = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest("sha256=" + mac, sig)


@dataclass
class Received:
    delivery_id: str
    event: str
    seq: int | None
    signature_ok: bool
    status: int
    payload: dict[str, Any]
    received_at: str


@dataclass
class ReceiverState:
    secret: str = ""
    #: 前 N 次投递故意返回 500（0 = 一直正常）
    fail_first: int = 0
    failures: int = 0
    received: list[Received] = field(default_factory=list)

    def reset(self, *, secret: str | None = None, fail_first: int | None = None) -> None:
        if secret is not None:
            self.secret = secret
        if fail_first is not None:
            self.fail_first = fail_first
        self.failures = 0
        self.received.clear()

    def note(self, record: Received) -> None:
        self.received.append(record)


STATE = ReceiverState(
    secret=os.environ.get("AICS_E2E_WEBHOOK_SECRET", ""),
    fail_first=int(os.environ.get("AICS_E2E_WEBHOOK_FAIL_FIRST", "0")),
)
app = FastAPI(title="AICS Webhook 示例接收端", docs_url=None, redoc_url=None)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"ok": True, "secret_set": bool(STATE.secret), "fail_first": STATE.fail_first}


@app.get("/received")
async def received() -> dict[str, Any]:
    return {
        "count": len(STATE.received),
        "failures": STATE.failures,
        "items": [asdict(r) for r in STATE.received],
    }


@app.post("/reset")
async def reset(request: Request) -> dict[str, Any]:
    body = await request.json() if await request.body() else {}
    STATE.reset(secret=body.get("secret"), fail_first=body.get("fail_first"))
    return {"ok": True, "fail_first": STATE.fail_first}


@app.post("/hook")
async def hook(request: Request) -> JSONResponse:
    body = await request.body()
    ts = request.headers.get(TIMESTAMP_HEADER, "")
    sig = request.headers.get(SIGNATURE_HEADER, "")
    delivery_id = request.headers.get(DELIVERY_HEADER, "")
    event = request.headers.get(EVENT_HEADER, "")
    try:
        payload: dict[str, Any] = json.loads(body)
    except ValueError:
        payload = {}
    ok = bool(ts) and bool(sig) and verify(STATE.secret, ts, body, sig)

    if not ok:
        status = 401
    elif STATE.failures < STATE.fail_first:
        STATE.failures += 1
        status = 500  # 故意失败：演示退避重试
    else:
        status = 200
    STATE.note(
        Received(
            delivery_id=delivery_id,
            event=event,
            seq=payload.get("seq"),
            signature_ok=ok,
            status=status,
            payload=payload,
            received_at=datetime.now(UTC).isoformat(),
        )
    )
    if status == 401:
        return JSONResponse({"ok": False, "error": "签名校验失败"}, status_code=status)
    return JSONResponse({"ok": status == 200, "status": status}, status_code=status)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def main() -> None:
    port = int(os.environ.get("AICS_E2E_WEBHOOK_PORT") or free_port())
    print(f"示例接收端：http://127.0.0.1:{port}/hook（密钥已设置：{bool(STATE.secret)}）")
    uvicorn.run(app, host="127.0.0.1", port=port, access_log=False, log_level="warning")


if __name__ == "__main__":  # pragma: no cover
    main()
