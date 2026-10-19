"""M4.1 ②：argon2id 哈希与权限矩阵（`modules.auth.passwords` / `.permissions`）。"""

from __future__ import annotations

from aics.modules.auth import passwords
from aics.modules.auth.permissions import (
    ALL_PERMISSIONS,
    APP_ADMIN_PERMISSIONS,
    ROLE_PERMISSIONS,
    VIEWER_PERMISSIONS,
    granted,
    permissions_for,
)


def test_hash_is_salted_and_verifies() -> None:
    first = passwords.hash_password("correct horse battery")
    second = passwords.hash_password("correct horse battery")
    assert first != second  # 每次加盐
    assert first.startswith("$argon2id$")
    assert passwords.verify_password("correct horse battery", first)
    assert not passwords.verify_password("wrong password", first)


def test_broken_hash_returns_false_instead_of_raising() -> None:
    assert not passwords.verify_password("whatever", "not-a-hash")


def test_fresh_hash_needs_no_rehash() -> None:
    assert passwords.needs_rehash(passwords.hash_password("abcdefgh")) is False


def test_permission_matrix_is_consistent() -> None:
    assert len(ALL_PERMISSIONS) == 25
    assert len(set(ALL_PERMISSIONS)) == 25
    assert set(APP_ADMIN_PERMISSIONS) == set(ALL_PERMISSIONS) - {
        "app.create",
        "app.delete",
        "app.copy",
        "user.admin",
        "system.read",
        "system.write",
    }
    assert set(VIEWER_PERMISSIONS) <= set(APP_ADMIN_PERMISSIONS)
    assert "system.write" not in VIEWER_PERMISSIONS
    assert set(ROLE_PERMISSIONS) == {"super_admin", "app_admin", "viewer"}


def test_permissions_for_and_granted() -> None:
    assert permissions_for(None) == frozenset()
    assert permissions_for("nope") == frozenset()
    assert "kb.write" in permissions_for("app_admin")
    assert "kb.write" not in permissions_for("viewer")
    assert granted(permissions_for("viewer"), "log.read")
    assert not granted(permissions_for("viewer"), "key.write")
