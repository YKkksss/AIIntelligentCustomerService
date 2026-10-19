"""开放接口清单（M3.1 ③、M3.3 ④）：跨应用隔离用例与路由守卫共用的单一事实来源。

开放接口分两类，两类都受清单守卫（`tests/unit/test_open_api_coverage.py`）约束：

1. **应用作用域**（`OPEN_API`）：挂在 `/api/v1/apps/{app_id}/...` 下，路径里带应用 id；
   跨应用由认证阶段的 404 `APP_NOT_FOUND` 收口，资源级再加 404 `CONVERSATION_NOT_FOUND`
   / `DOCUMENT_NOT_FOUND`；
2. **Key 作用域**（`OPEN_API_V1`）：`/v1/...`（OpenAI 兼容）路径里没有应用 id，身份来自 API Key，
   `model` 必须等于 Key 所属应用，否则 404 `model_not_found`。

新增接口时：在这里补一行（否则 CI 失败），并在 `tests/integration/test_isolation_pg.py` 补
对应的隔离用例（设计讨论 0009 D22）。

`{app_id}`、`{conversation_id}`、`{document_id}` 与 `{message_id}` 由测试填充；`body` 是请求体模板（GET 为 None）。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

#: 受清单守卫约束的前缀（应用作用域 + Key 作用域）
OPEN_API_PREFIXES = ("/api/v1/apps/", "/v1/")
#: 应用作用域前缀（历史引用）
OPEN_API_PREFIX = OPEN_API_PREFIXES[0]
_SKIP_METHODS = frozenset({"HEAD", "OPTIONS"})

OPEN_API: list[tuple[str, str, dict[str, Any] | None]] = [
    ("POST", "/api/v1/apps/{app_id}/chat", {"question": "退货怎么退？"}),
    ("GET", "/api/v1/apps/{app_id}/conversations/{conversation_id}/messages", None),
    ("GET", "/api/v1/apps/{app_id}/conversations/{conversation_id}/events", None),
    ("GET", "/api/v1/apps/{app_id}/conversations/{conversation_id}/handoff", None),
    (
        "POST",
        "/api/v1/apps/{app_id}/conversations/{conversation_id}/handoff/accept",
        {"agent_id": "a_001"},
    ),
    (
        "POST",
        "/api/v1/apps/{app_id}/conversations/{conversation_id}/handoff/messages",
        {"agent_id": "a_001", "content": "您好，我是人工客服"},
    ),
    (
        "POST",
        "/api/v1/apps/{app_id}/conversations/{conversation_id}/handoff/release",
        {"agent_id": "a_001"},
    ),
    ("GET", "/api/v1/apps/{app_id}/handoffs", None),
    ("POST", "/api/v1/apps/{app_id}/messages/{message_id}/feedback", {"rating": -1}),
    ("GET", "/api/v1/apps/{app_id}/documents", None),
    ("POST", "/api/v1/apps/{app_id}/documents", None),  # multipart：由隔离用例单独构造
    ("POST", "/api/v1/apps/{app_id}/documents/url", {"url": "https://example.com/a.md"}),
    ("GET", "/api/v1/apps/{app_id}/documents/{document_id}", None),
    ("PATCH", "/api/v1/apps/{app_id}/documents/{document_id}", {"title": "改名"}),
    ("DELETE", "/api/v1/apps/{app_id}/documents/{document_id}", None),
    ("GET", "/api/v1/apps/{app_id}/documents/{document_id}/chunks", None),
    ("PUT", "/api/v1/apps/{app_id}/documents/{document_id}/file", None),  # multipart
    ("POST", "/api/v1/apps/{app_id}/documents/{document_id}/reindex", None),
]

#: Key 作用域接口（OpenAI 兼容）：路径里没有应用 id，隔离语义是「Key 只能以自己的应用身份说话」
OPEN_API_V1: list[tuple[str, str]] = [
    ("GET", "/v1/models"),
    ("POST", "/v1/chat/completions"),
]


def declared_routes() -> set[tuple[str, str]]:
    """`(method, path)` of every interface registered in this manifest."""
    return {(method, path) for method, path, _ in OPEN_API} | set(OPEN_API_V1)


def open_routes(app: FastAPI) -> set[tuple[str, str]]:
    """`(method, path)` of every open-API route actually mounted on the app.

    Read from the generated OpenAPI schema: newer Starlette keeps included routers nested
    (`_IncludedRouter`), so walking `app.routes` no longer sees the mounted paths.
    """
    found: set[tuple[str, str]] = set()
    for path, operations in app.openapi().get("paths", {}).items():
        if not path.startswith(OPEN_API_PREFIXES):
            continue
        for method in operations:
            if method.upper() not in _SKIP_METHODS:
                found.add((method.upper(), path))
    return found
