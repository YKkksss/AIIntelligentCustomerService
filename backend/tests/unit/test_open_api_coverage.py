"""M3.1 ③ 开放接口清单守卫（设计讨论 0009 D22）。

任何一个新增的 `/api/v1/apps/{app_id}/...` 开放接口，如果没登记到 `tests/open_api.py`
的 `OPEN_API`，这里就会失败；跨应用隔离用例在 `tests/integration/test_isolation_pg.py`，
两者共用同一张表，所以不会出现“接口加了、隔离用例忘了”的情况。
"""

from __future__ import annotations

from aics.main import create_app
from tests.open_api import declared_routes, open_routes


def test_every_open_route_is_declared() -> None:
    missing = open_routes(create_app()) - declared_routes()

    assert not missing, (
        "新增开放接口未登记到 tests/open_api.py 的 OPEN_API："
        f"{sorted(missing)}；同时补 tests/integration/test_isolation_pg.py 的跨应用 404 用例"
    )


def test_declared_routes_still_exist() -> None:
    stale = declared_routes() - open_routes(create_app())

    assert not stale, f"OPEN_API 里登记的接口已经不存在：{sorted(stale)}"
