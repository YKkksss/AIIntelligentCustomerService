"""JWT 签发与校验（HS256；access 30 分钟、refresh 7 天，安全设计 §1）。

Claims：`sub`（用户 id）、`tv`（`users.token_version`，改密 / 禁用会 +1，旧 token 立即失效）、
`jti`（refresh 轮换与登出用）、`typ`（`access` / `refresh`）、`iat` / `exp`。

过期抛 `TokenExpiredError`（401 `TOKEN_EXPIRED`，前端据此刷新一次）；其余非法 token 一律
`UnauthenticatedError`（401 `UNAUTHENTICATED`）——不区分"签名错"与"格式错"，免得给攻击者提示。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt

from aics.core.errors import TokenExpiredError, UnauthenticatedError

ALGORITHM = "HS256"
ACCESS = "access"
REFRESH = "refresh"


@dataclass(frozen=True)
class Payload:
    user_id: UUID
    token_version: int
    kind: str
    jti: str
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class Issued:
    token: str
    payload: Payload


def issue(
    *,
    secret: str,
    user_id: UUID,
    token_version: int,
    kind: str,
    ttl: timedelta,
    now: datetime | None = None,
) -> Issued:
    issued_at = now or datetime.now(UTC)
    expires_at = issued_at + ttl
    jti = uuid.uuid4().hex
    claims: dict[str, Any] = {
        "sub": str(user_id),
        "tv": token_version,
        "typ": kind,
        "jti": jti,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    token = jwt.encode(claims, secret, algorithm=ALGORITHM)
    return Issued(
        token=token,
        payload=Payload(
            user_id=user_id,
            token_version=token_version,
            kind=kind,
            jti=jti,
            issued_at=issued_at,
            expires_at=expires_at,
        ),
    )


def decode(*, secret: str, token: str, kind: str) -> Payload:
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[ALGORITHM],
            options={"require": ["sub", "tv", "typ", "jti", "iat", "exp"]},
        )
    except jwt.ExpiredSignatureError as e:
        raise TokenExpiredError() from e
    except jwt.InvalidTokenError as e:
        raise UnauthenticatedError(details={"reason": "invalid_token"}) from e
    if claims.get("typ") != kind:
        raise UnauthenticatedError(details={"reason": "wrong_token_type"})
    try:
        return Payload(
            user_id=UUID(str(claims["sub"])),
            token_version=int(claims["tv"]),
            kind=kind,
            jti=str(claims["jti"]),
            issued_at=datetime.fromtimestamp(int(claims["iat"]), UTC),
            expires_at=datetime.fromtimestamp(int(claims["exp"]), UTC),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise UnauthenticatedError(details={"reason": "malformed_claims"}) from e
