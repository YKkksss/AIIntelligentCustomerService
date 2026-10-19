"""后台 RBAC 权限名与角色矩阵（安全设计 §2.1）。

权限名是**点分**格式（`app.write`），与开放接口的 API Key scope（`kb:write`）是两套命名，永远不
混用。`roles.permissions` 是库里的种子数据（迁移 `0001`），这里只是同一份矩阵的代码副本——
`tests/unit/test_permissions.py` 把两者逐项比对，文档、种子、代码三处一旦分叉就会红。

`super_admin` 由 `users.is_super_admin` 表示，不查 `user_app_roles`；应用内的 `app_admin` / `viewer`
查 `user_app_roles.role_id`。
"""

from __future__ import annotations

from collections.abc import Iterable

ALL_PERMISSIONS: tuple[str, ...] = (
    "app.read",
    "app.write",
    "app.create",
    "app.delete",
    "app.copy",
    "member.write",
    "kb.read",
    "kb.write",
    "chat.debug",
    "context.compact",
    "key.read",
    "key.write",
    "webhook.write",
    "handoff.release",
    "callback.clear",
    "log.read",
    "feedback.write",
    "eval.read",
    "eval.write",
    "eval.run",
    "stats.read",
    "audit.read",
    "user.admin",
    "system.read",
    "system.write",
)

#: app_admin 没有的权限（全局动作与平台设置）。
_NOT_APP_ADMIN = frozenset(
    {"app.create", "app.delete", "app.copy", "user.admin", "system.read", "system.write"}
)

APP_ADMIN_PERMISSIONS: tuple[str, ...] = tuple(
    p for p in ALL_PERMISSIONS if p not in _NOT_APP_ADMIN
)
VIEWER_PERMISSIONS: tuple[str, ...] = (
    "app.read",
    "kb.read",
    "chat.debug",
    "log.read",
    "eval.read",
    "stats.read",
)

ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "super_admin": ALL_PERMISSIONS,
    "app_admin": APP_ADMIN_PERMISSIONS,
    "viewer": VIEWER_PERMISSIONS,
}

#: 只读地看别的应用的审计需要 `audit.read` + 超级管理员；app_admin 只看本应用。
PLATFORM_ADMIN_ROLES = frozenset({"super_admin"})


def permissions_for(role_id: str | None) -> frozenset[str]:
    if role_id is None:
        return frozenset()
    return frozenset(ROLE_PERMISSIONS.get(role_id, ()))


def granted(permissions: Iterable[str], wanted: str) -> bool:
    return wanted in permissions
