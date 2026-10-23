"""M4.7 ⑤：公共知识库的权限口径（创建者 / 超级管理员才可运营）。"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from aics.core.errors import ForbiddenError
from aics.modules.knowledge.kb_service import ensure_kb_manager


def _kb(owner):
    return SimpleNamespace(id=uuid4(), name="售后知识库", created_by=owner)


def test_creator_can_manage() -> None:
    owner = uuid4()
    ensure_kb_manager(_kb(owner), actor_id=owner, is_super_admin=False)


def test_super_admin_can_manage_any_kb() -> None:
    ensure_kb_manager(_kb(uuid4()), actor_id=uuid4(), is_super_admin=True)


def test_other_user_is_rejected() -> None:
    kb = _kb(uuid4())
    with pytest.raises(ForbiddenError) as err:
        ensure_kb_manager(kb, actor_id=uuid4(), is_super_admin=False)
    assert err.value.status == 403
    assert "创建者" in err.value.message


def test_anonymous_is_rejected() -> None:
    with pytest.raises(ForbiddenError):
        ensure_kb_manager(_kb(uuid4()), actor_id=None, is_super_admin=False)


def test_kb_without_owner_only_super_admin() -> None:
    kb = _kb(None)
    ensure_kb_manager(kb, actor_id=uuid4(), is_super_admin=True)
    with pytest.raises(ForbiddenError):
        ensure_kb_manager(kb, actor_id=uuid4(), is_super_admin=False)
