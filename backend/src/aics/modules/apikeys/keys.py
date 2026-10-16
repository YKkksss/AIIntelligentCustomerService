"""API key format and hashing (安全设计 §1).

Format `aics_` + 32 base62 characters (≈190 bit). Stored: `prefix` (first 12 characters,
unique, for lookup and display) and `HMAC-SHA256(pepper, key)` as hex; compared with
`hmac.compare_digest`.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import string

KEY_PREFIX = "aics_"
KEY_RANDOM_CHARS = 32
PREFIX_CHARS = 12  # "aics_" + 7 random characters
_ALPHABET = string.ascii_letters + string.digits
_KEY_RE = re.compile(rf"^{KEY_PREFIX}[0-9A-Za-z]{{{KEY_RANDOM_CHARS}}}$")


def generate_key() -> str:
    return KEY_PREFIX + "".join(secrets.choice(_ALPHABET) for _ in range(KEY_RANDOM_CHARS))


def key_prefix(key: str) -> str:
    return key[:PREFIX_CHARS]


def is_well_formed(key: str) -> bool:
    return bool(_KEY_RE.match(key))


def hash_key(key: str, pepper: str) -> str:
    return hmac.new(pepper.encode(), key.encode(), hashlib.sha256).hexdigest()


def matches(key: str, key_hash: str, pepper: str) -> bool:
    return hmac.compare_digest(hash_key(key, pepper), key_hash)


def parse_bearer(authorization: str | None) -> str | None:
    """`Authorization: Bearer <key>` → key, or None when missing / malformed."""
    if not authorization:
        return None
    scheme, _, value = authorization.strip().partition(" ")
    value = value.strip()
    if scheme.lower() != "bearer" or not is_well_formed(value):
        return None
    return value
