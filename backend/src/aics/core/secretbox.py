"""Webhook 签名密钥的生成、加密与解密（Fernet，主密钥 `AICS_DATA_ENCRYPTION_KEY`）。

放在 `core` 而不是 `modules/webhooks`：`modules.apps`（保存 / 轮换密钥）与 `modules.webhooks`
（投递时解密）都要用它，而这两个模块在 import-linter 里是「互不依赖」的独立契约
（`pyproject.toml` 第 8 条合同），谁也不能 import 谁。

明文密钥只在生成 / 轮换的那一次返回给调用方，库里存 `secret_enc`（密文）与 `secret_prefix`
（可展示的前缀，日志与接口只出现它）。
"""

from __future__ import annotations

import base64
import hashlib
import secrets

from cryptography.fernet import Fernet, InvalidToken

#: 密钥明文的前缀（第三方一眼能看出这是 AICS 的签名密钥）。
SECRET_PREFIX = "whsec_"  # noqa: S105
#: `secret_prefix` 的长度：`whsec_` + 8 位随机（够区分轮换过几次，又不泄露密钥本体）。
PREFIX_LENGTH = len(SECRET_PREFIX) + 8


def _fernet(key: str) -> Fernet:
    """把配置里的主密钥变成 Fernet key。

    既接受标准 Fernet key（44 字符 base64，解码后 32 字节），也接受任意随机串——后者用
    SHA-256 派生，免得运维必须先生成一个特定格式的串才能起服务。
    """
    raw = key.encode()
    if len(raw) == 44:
        try:
            if len(base64.urlsafe_b64decode(raw)) == 32:
                return Fernet(raw)
        except (ValueError, TypeError):  # 不是合法 base64：退回派生
            pass
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(raw).digest()))


def new_secret() -> str:
    """生成一个签名密钥明文（`whsec_` + 32 字节随机，base64url 无填充）。"""
    return f"{SECRET_PREFIX}{secrets.token_urlsafe(32)}"


def prefix_of(secret: str) -> str:
    """可展示的前缀（日志、接口只出现这个）。"""
    return secret[:PREFIX_LENGTH]


def encrypt(plain: str, key: str) -> str:
    """明文密钥 → 入库的密文。"""
    return _fernet(key).encrypt(plain.encode()).decode()


def decrypt(token: str, key: str) -> str:
    """入库的密文 → 明文密钥；主密钥不匹配时抛 `InvalidToken`。"""
    return _fernet(key).decrypt(token.encode()).decode()


__all__ = [
    "PREFIX_LENGTH",
    "SECRET_PREFIX",
    "InvalidToken",
    "decrypt",
    "encrypt",
    "new_secret",
    "prefix_of",
]
