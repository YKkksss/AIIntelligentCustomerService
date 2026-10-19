"""第三方接入指南 §7.4「Python 校验示例」的原样搬运。

这段代码是**文档交给接收方抄的**参照实现；测试用它验证我们的签名，等于同时验证
「文档写对了」与「实现按文档做了」。两边任何一处改了签名式（例如换算法、换拼接顺序、
去掉 `sha256=` 前缀）都会让 `tests/unit/test_webhook_contract.py` 失败。
"""

from __future__ import annotations

import hashlib
import hmac
import time


def verify(secret: str, ts: str, body: bytes, sig: str) -> bool:
    if abs(time.time() - int(ts)) > 300:
        return False
    mac = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest("sha256=" + mac, sig)
