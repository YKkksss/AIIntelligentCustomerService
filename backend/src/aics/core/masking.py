"""PII masking for logs, traces and metric labels (对话上下文管理 §13).

Phone numbers keep the first 3 and last 4 digits; order numbers keep the last 4 characters.
"""

from __future__ import annotations

import re

_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d)(\d{4})(\d{4})(?!\d)")


def mask_phone(value: str) -> str:
    v = value.strip()
    if len(v) <= 7:
        return "*" * len(v)
    return v[:3] + "*" * (len(v) - 7) + v[-4:]


def mask_order(value: str) -> str:
    v = value.strip()
    if len(v) <= 4:
        return "*" * len(v)
    return "*" * (len(v) - 4) + v[-4:]


def mask_identifier(kind: str, value: str) -> str:
    """`kind` is "phone" or "order"; anything else keeps the last 4 characters."""
    return mask_phone(value) if kind == "phone" else mask_order(value)


def mask_fact(key: str, value: str) -> str:
    """Fact values in traces / logs: phone and order_no are masked, others kept."""
    if key == "phone":
        return mask_phone(value)
    if key == "order_no":
        return mask_order(value)
    return value


def mask_text(text: str) -> str:
    """Mask phone-number-looking substrings inside free text."""
    return _PHONE_RE.sub(lambda m: m.group(1) + "****" + m.group(3), text)
