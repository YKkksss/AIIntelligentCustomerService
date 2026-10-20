"""`bootstrap-admin` 的默认超管约定（本地默认口令 + 生产拒绝）。"""

from __future__ import annotations

import argparse

import pytest

from aics.cli import _bootstrap_admin
from aics.core import config as config_module


def _args(email: str | None = None, password: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(email=email, password=password, name=None)


def _prod_settings() -> config_module.Settings:
    real = config_module.get_settings.__wrapped__()  # type: ignore[attr-defined]
    return real.model_copy(update={"env": "prod"})


def test_default_admin_credentials_are_documented_values() -> None:
    s = config_module.get_settings()
    assert s.bootstrap_admin_email == config_module.DEFAULT_ADMIN_EMAIL
    assert s.bootstrap_admin_password is not None
    assert s.bootstrap_admin_password.get_secret_value() == config_module.DEFAULT_ADMIN_PASSWORD


@pytest.mark.asyncio
async def test_prod_rejects_builtin_default_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = _prod_settings()
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)

    code = await _bootstrap_admin(_args())

    assert code == 2
    assert "生产环境禁止使用内置默认超管口令" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_prod_rejects_default_email_even_with_custom_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _prod_settings()
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)

    code = await _bootstrap_admin(_args(password="Str0ng-Passw0rd!"))

    assert code == 2
