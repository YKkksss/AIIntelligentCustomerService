"""M4.1 ②：JWT 签发 / 校验（`modules.auth.tokens`）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from aics.core.errors import TokenExpiredError, UnauthenticatedError
from aics.modules.auth import tokens

SECRET = "s" * 64
USER = uuid4()


def _issue(*, kind: str = tokens.ACCESS, ttl: timedelta = timedelta(minutes=30)):
    return tokens.issue(secret=SECRET, user_id=USER, token_version=3, kind=kind, ttl=ttl)


def test_issue_then_decode_roundtrip() -> None:
    issued = _issue()
    payload = tokens.decode(secret=SECRET, token=issued.token, kind=tokens.ACCESS)
    assert payload.user_id == USER
    assert payload.token_version == 3
    assert payload.kind == tokens.ACCESS
    assert payload.jti == issued.payload.jti
    assert payload.expires_at > payload.issued_at


def test_expired_token_is_token_expired() -> None:
    issued = tokens.issue(
        secret=SECRET,
        user_id=USER,
        token_version=0,
        kind=tokens.ACCESS,
        ttl=timedelta(seconds=30),
        now=datetime.now(UTC) - timedelta(minutes=5),
    )
    with pytest.raises(TokenExpiredError) as err:
        tokens.decode(secret=SECRET, token=issued.token, kind=tokens.ACCESS)
    assert err.value.status == 401
    assert err.value.code == "TOKEN_EXPIRED"


def test_wrong_token_kind_is_rejected() -> None:
    issued = _issue(kind=tokens.REFRESH)
    with pytest.raises(UnauthenticatedError) as err:
        tokens.decode(secret=SECRET, token=issued.token, kind=tokens.ACCESS)
    assert err.value.details["reason"] == "wrong_token_type"


def test_tampered_token_is_rejected() -> None:
    good = _issue().token
    with pytest.raises(UnauthenticatedError):
        tokens.decode(secret=SECRET, token=good[:-2] + "xx", kind=tokens.ACCESS)


def test_garbage_token_is_rejected() -> None:
    with pytest.raises(UnauthenticatedError) as err:
        tokens.decode(secret=SECRET, token="not-a-jwt", kind=tokens.ACCESS)
    assert err.value.details["reason"] == "invalid_token"


def test_wrong_secret_is_rejected() -> None:
    issued = _issue()
    with pytest.raises(UnauthenticatedError):
        tokens.decode(secret="x" * 64, token=issued.token, kind=tokens.ACCESS)
