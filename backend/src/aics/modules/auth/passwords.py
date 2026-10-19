"""密码哈希（argon2id，安全设计 §1 / 数据库设计 §3.1）。

参数取 OWASP 建议的"第二档"（19 MiB 内存 / 2 次迭代起）：后台登录频率很低，宁可让单次验证
慢一点，也不要为了省 CPU 降低抗暴力破解强度。`verify_password` 对"哈希格式坏了"也返回 False
（而不是抛异常），这样库里一条脏数据不会把登录接口变成 500。
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_HASHER = PasswordHasher(
    time_cost=3,
    memory_cost=65536,  # 64 MiB
    parallelism=4,
    hash_len=32,
    salt_len=16,
)


def hash_password(plain: str) -> str:
    return _HASHER.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _HASHER.verify(hashed, plain)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(hashed: str) -> bool:
    """True when the stored hash was produced with older parameters (rehash on login)."""
    try:
        return _HASHER.check_needs_rehash(hashed)
    except InvalidHashError:
        return True
