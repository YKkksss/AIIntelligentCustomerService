"""审计端口（ADR-0017）：`AuditSink` 由 `modules.audit` 实现，`aics.main` 注入 `app.state.audit`。

业务 service 在自己**同一条事务**里调用 `sink.write(session, entry)`：写操作与审计要么一起成功，
要么一起回滚（M4.1 的集成用例专门验证这一点）。`modules.auth` 与 `modules.audit` 互不依赖，
所以端口放在 `core`；哪些动作要审计由调用方决定，端口只负责落库与脱敏。

`redact()` 在入口统一处理敏感字段：密码、密钥、token 一律替换成 `"***"`，`diff` 里不出现明文。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

REDACTED = "***"
#: 完全匹配即脱敏（大小写不敏感）。
SENSITIVE_KEYS = frozenset(
    {
        "password",
        "password_hash",
        "new_password",
        "current_password",
        "secret",
        "secret_enc",
        "api_key",
        "access_token",
        "refresh_token",
        "token",
        "authorization",
    }
)
#: 名字里出现这些片段也脱敏（`webhook_config` 里的 `secret_prefix` 保留，便于排障）。
SENSITIVE_FRAGMENTS = ("secret", "password", "api_key")


@dataclass(frozen=True)
class AuditEntry:
    """一条审计事件；`action` 用点分动词，如 `app.update`、`kb.document.delete`。"""

    action: str
    actor_type: str = "user"
    actor_id: UUID | None = None
    app_id: UUID | None = None
    target_type: str | None = None
    target_id: UUID | None = None
    diff: dict[str, Any] | None = field(default=None)
    ip: str | None = None
    user_agent: str | None = None
    request_id: str | None = None


class AuditSink(Protocol):
    async def write(self, session: AsyncSession, entry: AuditEntry) -> None:
        """Write one entry inside the caller's transaction (no commit here)."""
        ...


def _sensitive(key: str) -> bool:
    lowered = key.lower()
    if lowered in SENSITIVE_KEYS:
        return True
    return any(fragment in lowered for fragment in SENSITIVE_FRAGMENTS)


def redact(value: Any) -> Any:
    """Recursively replace sensitive values; keeps structure so diffs stay readable."""
    if isinstance(value, Mapping):
        return {
            key: REDACTED if _sensitive(str(key)) and item is not None else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [redact(item) for item in value]
    return value


def diff(before: Mapping[str, Any] | None, after: Mapping[str, Any] | None) -> dict[str, Any]:
    """Compute `{"before": …, "after": …}` for keys that actually changed, redacted."""
    before = before or {}
    after = after or {}
    changed_before: dict[str, Any] = {}
    changed_after: dict[str, Any] = {}
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        changed_before[key] = redact(old)
        changed_after[key] = redact(new)
    return {"before": changed_before, "after": changed_after}
