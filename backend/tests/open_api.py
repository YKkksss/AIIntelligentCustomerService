"""开放接口清单（M3.1 ③）：跨应用隔离用例与路由守卫共用的单一事实来源。

新增一个挂在 `/api/v1/apps/{app_id}/...` 下的开放接口时，必须：

1. 在本文件的 `OPEN_API` 里补一行（否则 `tests/unit/test_open_api_coverage.py` 会让 CI 失败）；
2. 在 `tests/integration/test_isolation_pg.py` 里补资源级 404 用例（设计讨论 0009 D22）。

`{app_id}` 与 `{conversation_id}` 由测试填充；`body` 是请求体模板（GET 为 None）。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

OPEN_API_PREFIX = "/api/v1/apps/"
_SKIP_METHODS = frozenset({"HEAD", "OPTIONS"})

OPEN_API: list[tuple[str, str, dict[str, Any] | None]] = [
    ("POST", "/api/v1/apps/{app_id}/chat", {"question": "退货怎么退？"}),
    ("GET", "/api/v1/apps/{app_id}/conversations/{conversation_id}/messages", None),
]


def declared_routes() -> set[tuple[str, str]]:
    """`(method, path)` of every interface registered in OPEN_API."""
    return {(method, path) for method, path, _ in OPEN_API}


def open_routes(app: FastAPI) -> set[tuple[str, str]]:
    """`(method, path)` of every open-API route actually mounted on the app.

    Read from the generated OpenAPI schema: newer Starlette keeps included routers nested
    (`_IncludedRouter`), so walking `app.routes` no longer sees the mounted paths.
    """
    found: set[tuple[str, str]] = set()
    for path, operations in app.openapi().get("paths", {}).items():
        if not path.startswith(OPEN_API_PREFIX):
            continue
        for method in operations:
            if method.upper() not in _SKIP_METHODS:
                found.add((method.upper(), path))
    return found
