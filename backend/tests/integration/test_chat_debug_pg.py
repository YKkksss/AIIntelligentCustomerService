"""M4.4 ② (integration): the admin debug endpoints over real PG + fake models.

What only a real database can prove: the debug turn is persisted as `channel="debug"` with
the retrieval trace in `messages.retrieval_trace`, the trace a viewer reads is the same one,
forced compaction leaves a `jobs(type=compact)` row, and a rejected `debug_config` creates
nothing.
"""

from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select
from tests.chat_support import make_runtime

from aics.core.errors import ForbiddenError, UnauthenticatedError
from aics.core.security import AuthedUser
from aics.db.models import Conversation, Job, Message
from aics.main import create_app
from aics.memory.compactor import CompactResult

pytestmark = pytest.mark.integration

HEADERS = {"authorization": "Bearer good-token"}
PHONE_QUESTION = "我的手机号 13812345678，签收后还能退货吗"


class FakeAuthorizer:
    def __init__(self, user_id: UUID) -> None:
        self.user_id = user_id
        self.denied: set[str] = set()

    async def authenticate(self, authorization: str | None) -> AuthedUser:
        if authorization != "Bearer good-token":
            raise UnauthenticatedError(details={"reason": "missing_token"})
        return AuthedUser(
            id=self.user_id,
            email="admin@example.com",
            display_name="管理员",
            is_super_admin=False,
        )

    async def authorize(self, user: Any, *, app_id: UUID | None, permission: str) -> None:
        if permission in self.denied:
            raise ForbiddenError(details={"reason": "missing_permission", "permission": permission})


class StubCompactor:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, UUID]] = []

    async def run(self, app_id: UUID, conversation_id: UUID, params: Any) -> CompactResult:
        self.calls.append((app_id, conversation_id))
        return CompactResult(
            result="success",
            version=7,
            batches=2,
            until_msg_id=uuid.uuid4(),
            facts=[],
            usage={"prompt": 12, "completion": 6},
        )


@pytest.fixture
def debug_app(pg: dict[str, Any]) -> dict[str, Any]:
    app = create_app()
    runtime = make_runtime(pg["sessions"])
    compactor = StubCompactor()
    runtime.memory.compactor = compactor  # type: ignore[attr-defined]
    authorizer = FakeAuthorizer(pg["user_id"])
    app.state.chat_runtime = runtime
    app.state.authorizer = authorizer
    return {"app": app, "runtime": runtime, "authorizer": authorizer, "compactor": compactor}


async def turn(client: httpx.AsyncClient, app_id: UUID, body: dict[str, Any]) -> str:
    async with client.stream(
        "POST", f"/api/v1/admin/apps/{app_id}/debug/chat", json=body, headers=HEADERS
    ) as resp:
        assert resp.status_code == 200, await resp.aread()
        return "".join([chunk async for chunk in resp.aiter_text()])


async def test_debug_turn_streams_trace_and_persists(debug_app: dict[str, Any], pg: dict) -> None:
    a = pg["app_a"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=debug_app["app"]), base_url="http://t"
    ) as client:
        body = await turn(
            client, a, {"question": PHONE_QUESTION, "debug_config": {"vector_top_k": 5}}
        )

    for frame in ("event: start", "event: trace", "event: citations", "event: end"):
        assert frame in body, frame

    async with pg["sessions"]() as s:
        conv = (await s.execute(select(Conversation).where(Conversation.app_id == a))).scalar_one()
        assert conv.channel == "debug"
        assert conv.created_by == pg["user_id"]
        bot = (
            await s.execute(
                select(Message).where(
                    Message.conversation_id == conv.id, Message.role == "assistant"
                )
            )
        ).scalar_one()
        trace = bot.retrieval_trace
        assert trace is not None
        assert set(trace) >= {"retrieval", "timings"}
        assert trace["retrieval"]["query"] == PHONE_QUESTION
        # 调试参数只对本轮生效，没有写回应用配置
        from aics.modules.apps.service import AppService

        app_row = await AppService(s).get_app(a)
        assert AppService(s).config_of(app_row).rag.vector_top_k == 20

    await debug_app["runtime"].drain()


async def test_rejected_debug_config_creates_nothing(debug_app: dict[str, Any], pg: dict) -> None:
    a = pg["app_a"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=debug_app["app"]), base_url="http://t"
    ) as client:
        resp = await client.post(
            f"/api/v1/admin/apps/{a}/debug/chat",
            json={"question": "退货", "debug_config": {"vector_top_k": 0}},
            headers=HEADERS,
        )

    assert resp.status_code == 422
    errors = resp.json()["error"]["details"]["errors"]
    assert errors[0]["loc"] == ["debug_config", "vector_top_k"]

    async with pg["sessions"]() as s:
        count = await s.scalar(
            select(func.count()).select_from(Conversation).where(Conversation.app_id == a)
        )
    assert count == 0


async def test_viewer_reads_masked_history(debug_app: dict[str, Any], pg: dict) -> None:
    a = pg["app_a"]
    debug_app["authorizer"].denied = {"app.write"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=debug_app["app"]), base_url="http://t"
    ) as client:
        await turn(client, a, {"question": PHONE_QUESTION})
        async with pg["sessions"]() as s:
            cid = await s.scalar(select(Conversation.id).where(Conversation.app_id == a))
        resp = await client.get(
            f"/api/v1/admin/apps/{a}/debug/conversations/{cid}", headers=HEADERS
        )

    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items[0]["content"] == "我的手机号 138****5678，签收后还能退货吗"
    trace = next(i["trace"] for i in items if i["trace"] is not None)
    assert "138****5678" in trace["retrieval"]["query"]

    await debug_app["runtime"].drain()


async def test_app_member_reads_raw_history(debug_app: dict[str, Any], pg: dict) -> None:
    a = pg["app_a"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=debug_app["app"]), base_url="http://t"
    ) as client:
        await turn(client, a, {"question": PHONE_QUESTION})
        async with pg["sessions"]() as s:
            cid = await s.scalar(select(Conversation.id).where(Conversation.app_id == a))
        resp = await client.get(
            f"/api/v1/admin/apps/{a}/debug/conversations/{cid}", headers=HEADERS
        )

    assert resp.status_code == 200
    items = resp.json()["items"]
    assert "13812345678" in items[0]["content"]

    await debug_app["runtime"].drain()


async def test_compact_writes_job_row(debug_app: dict[str, Any], pg: dict) -> None:
    a = pg["app_a"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=debug_app["app"]), base_url="http://t"
    ) as client:
        await turn(client, a, {"question": PHONE_QUESTION})
        async with pg["sessions"]() as s:
            cid = await s.scalar(select(Conversation.id).where(Conversation.app_id == a))
        resp = await client.post(
            f"/api/v1/admin/apps/{a}/debug/compact",
            json={"conversation_id": str(cid)},
            headers=HEADERS,
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["compacted"] is True and body["batches"] == 2 and body["version"] == 7
    assert debug_app["compactor"].calls == [(a, cid)]

    async with pg["sessions"]() as s:
        job = await s.get(Job, UUID(body["job_id"]))
        assert job is not None
        assert (job.type, job.status, job.target_id) == ("compact", "done", cid)
        assert job.created_by == pg["user_id"]
        assert job.finished_at is not None

    await debug_app["runtime"].drain()


async def test_compact_unknown_conversation_is_404(debug_app: dict[str, Any], pg: dict) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=debug_app["app"]), base_url="http://t"
    ) as client:
        resp = await client.post(
            f"/api/v1/admin/apps/{pg['app_a']}/debug/compact",
            json={"conversation_id": str(uuid.uuid4())},
            headers=HEADERS,
        )

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "CONVERSATION_NOT_FOUND"
