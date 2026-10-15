"""迁移可升级、可回滚，且与 ORM 模型一致。

运行：AICS_TEST_DB_URL=postgresql+asyncpg://aics:<pwd>@<host>:5432/aics_test uv run pytest -m integration
（会清空该库中的 AICS 表，只能指向测试库）。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

pytestmark = pytest.mark.integration

URL = os.environ.get("AICS_TEST_DB_URL")


@pytest.fixture
def alembic_cfg(monkeypatch: pytest.MonkeyPatch) -> Config:
    if not URL:
        pytest.skip("未设置 AICS_TEST_DB_URL")
    monkeypatch.setenv("AICS_DB_URL", URL)
    from aics.core.config import get_settings

    get_settings.cache_clear()
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


def test_upgrade_downgrade_roundtrip(alembic_cfg: Config) -> None:
    command.downgrade(alembic_cfg, "base")
    command.upgrade(alembic_cfg, "head")
    command.check(alembic_cfg)  # 模型与迁移无差异
    command.downgrade(alembic_cfg, "base")
    command.upgrade(alembic_cfg, "head")
