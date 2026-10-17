"""M3.1 ③ 跨应用隔离回归集（设计讨论 0009 D22、ADR-0002）。

两层隔离，全部以 404 收口（不泄露“这个应用/会话存在但你没权限”）：

1. **应用级**：A 应用的 Key 访问 B 应用的开放接口 → 认证阶段 404 `APP_NOT_FOUND`；
2. **资源级**：A 应用自己的 Key，带着 B 应用的资源 id 访问 A 的接口 → 404 `CONVERSATION_NOT_FOUND`。

用例由 `tests/open_api.py` 的 `OPEN_API` 驱动，新增开放接口时补一行即可；漏登记会被
`tests/unit/test_open_api_coverage.py`（默认单测里跑）挡住。最后一条用例是正向对照，
保证上面的 404 来自隔离而不是接口本身坏了。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from tests.chat_support import make_runtime
from tests.open_api import OPEN_API

from aics.core.config import get_settings
from aics.main import create_app
from aics.modules.apikeys.service import ApiKeyAuthenticator, ApiKeyService

pytestmark = pytest.mark.integration


async def new_key(pg: dict[str, Any], redis: Any, app_id: UUID) -> str:
    pepper = get_settings().api_key_pepper.get_secret_value()
    async with pg["sessions"]() as s:
        _, plain = await ApiKeyService(s, pepper=pepper, redis=redis).create(
            app_id, name="isolation-it", scopes=["chat"], rpm_limit=1000
        )
        await s.commit()
    return plain


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def build_app(pg: dict[str, Any], redis: Any) -> FastAPI:
    app = create_app()
    app.state.authenticator = ApiKeyAuthenticator(
        pg["sessions"],
        pepper=get_settings().api_key_pepper.get_secret_value(),
        redis=redis,
    )
    app.state.chat_runtime = make_runtime(pg["sessions"])
    return app


@pytest.fixture
async def iso(pg: dict[str, Any], redis_it: Any) -> AsyncIterator[dict[str, Any]]:
    app = build_app(pg, redis_it)
    key_a = await new_key(pg, redis_it, pg["app_a"])
    key_b = await new_key(pg, redis_it, pg["app_b"])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t", timeout=30) as client:
        # 两个应用各自真实存在一个会话：A 的用于正向对照，B 的充当“别人的资源”
        conv_a = await ask(client, pg["app_a"], key_a)
        conv_b = await ask(client, pg["app_b"], key_b)
        yield {
            "client": client,
            "app_a": pg["app_a"],
            "app_b": pg["app_b"],
            "key_a": key_a,
            "key_b": key_b,
            "conv_a": conv_a,
            "conv_b": conv_b,
        }
    await app.state.chat_runtime.drain()


async def ask(client: httpx.AsyncClient, app_id: UUID, key: str) -> UUID:
    r = await client.post(
        f"/api/v1/apps/{app_id}/chat", json={"question": "退款多久到账？"}, headers=auth(key)
    )
    assert r.status_code == 200, r.text
    return UUID(r.json()["conversation_id"])


@pytest.mark.parametrize("method,path,body", OPEN_API, ids=lambda v: str(v)[:44])
async def test_cross_app_key_is_404(
    iso: dict[str, Any], method: str, path: str, body: dict[str, Any] | None
) -> None:
    url = path.format(app_id=iso["app_b"], conversation_id=iso["conv_b"])

    r = await iso["client"].request(method, url, json=body, headers=auth(iso["key_a"]))

    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "APP_NOT_FOUND"


async def test_cross_app_conversation_is_404_on_messages(iso: dict[str, Any]) -> None:
    url = f"/api/v1/apps/{iso['app_a']}/conversations/{iso['conv_b']}/messages"

    r = await iso["client"].get(url, headers=auth(iso["key_a"]))

    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "CONVERSATION_NOT_FOUND"


async def test_cross_app_conversation_is_404_on_chat(iso: dict[str, Any]) -> None:
    url = f"/api/v1/apps/{iso['app_a']}/chat"

    r = await iso["client"].post(
        url,
        json={"question": "退款多久到账？", "conversation_id": str(iso["conv_b"])},
        headers=auth(iso["key_a"]),
    )

    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "CONVERSATION_NOT_FOUND"


async def test_cross_app_conversation_does_not_leak_messages(iso: dict[str, Any]) -> None:
    """反过来也不行：B 的 Key 读不到 A 的会话。"""
    url = f"/api/v1/apps/{iso['app_b']}/conversations/{iso['conv_a']}/messages"

    r = await iso["client"].get(url, headers=auth(iso["key_b"]))

    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "CONVERSATION_NOT_FOUND"


async def test_own_key_and_resource_still_works(iso: dict[str, Any]) -> None:
    url = f"/api/v1/apps/{iso['app_a']}/conversations/{iso['conv_a']}/messages"

    r = await iso["client"].get(url, headers=auth(iso["key_a"]))

    assert r.status_code == 200, r.text
    assert r.json()["conversation"]["id"] == str(iso["conv_a"])
    assert [m["role"] for m in r.json()["items"]] == ["user", "assistant"]
