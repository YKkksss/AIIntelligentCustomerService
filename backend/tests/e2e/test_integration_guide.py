"""接入指南示例的端到端验收（M3.6 ④）。

把 `docs/04-api/04-第三方接入指南.md` 的示例逐段跑一遍——文档里的代码能跑、能拿到预期字段，
才算「接入方照着抄不会卡住」：

| 用例 | 指南小节 | 跑的是什么 |
|---|---|---|
| `test_section_2_curl` | §2 | 阻塞问答、`-N` 流式、`-F` 上传（带 `Idempotency-Key`） |
| `test_section_3_python_httpx` | §3 | `httpx` 问答、拿 `conversation_id` 续聊、`iter_lines` 读 SSE |
| `test_section_4_openai_sdk` | §4 | 官方 SDK：`models.list`、阻塞、`X-AICS-Conversation-Id` 续聊、流式 |
| `test_section_5_sse_parsing` | §5 | `fetch` 的帧解析规则（`\\n\\n` 分帧、`event:` / `data:` 两行） |
| `test_section_7_4_signature` | §7.4 | 接收方验签示例：四头齐全、验签通过、篡改与过期失败 |

做法：真 uvicorn 起在本进程的事件循环里（不另开线程——引擎与会话工厂绑定在这个 loop 上，
换 loop 会炸），同步客户端（curl / httpx / OpenAI SDK）放进 `asyncio.to_thread`，
否则会阻塞服务端、请求互相等死。模型是 `tests/chat_support` 的脚本假模型，Qdrant 不参与，
上传只进 `KnowledgeRuntime` 的 `enqueue` 记录，因此真实模型费用为 0。

运行：`uv run --frozen pytest tests/e2e -m integration -q`（需要 `AICS_TEST_DB_URL` 与真 Redis）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from openai import OpenAI
from redis.asyncio import Redis
from tests.chat_support import make_runtime
from tests.webhook_support import verify as verify_like_the_guide

from aics.core.config import get_settings
from aics.main import create_app
from aics.modules.apikeys.service import ApiKeyAuthenticator, ApiKeyService
from aics.modules.knowledge.runtime import KnowledgeRuntime
from aics.modules.webhooks import payloads, signing
from aics.storage.local import LocalStorage

pytestmark = pytest.mark.integration

GUIDE = Path(__file__).resolve().parents[3] / "docs" / "04-api" / "04-第三方接入指南.md"
QUESTION = "退货要多久到账？"
FOLLOW_UP = "那运费谁出？"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _in_thread(fn: Callable[..., Any], *args: Any) -> Any:
    return await asyncio.to_thread(fn, *args)


def _url(base: str, app_id: str, path: str = "chat") -> str:
    return f"{base}/api/v1/apps/{app_id}/{path}"


@pytest.fixture
def upload_file(tmp_path: Path) -> Path:
    """上传用的小文件：sync 夹具里建，免得在 async 用例里做阻塞 IO。"""
    path = tmp_path / "guide.txt"
    path.write_text("退货政策：7 天内可退。", encoding="utf-8")
    return path


@pytest.fixture
async def live(
    pg: dict[str, Any], redis_it: Redis, tmp_path: Path
) -> AsyncIterator[dict[str, Any]]:
    """真 uvicorn（本进程 loop）+ 预装运行时；返回接入方需要的那三个值。"""
    app = create_app()
    pepper = get_settings().api_key_pepper.get_secret_value()
    app.state.authenticator = ApiKeyAuthenticator(pg["sessions"], pepper=pepper, redis=redis_it)
    chat_runtime = make_runtime(pg["sessions"])
    app.state.chat_runtime = chat_runtime
    uploads: list[tuple[Any, ...]] = []
    # 上传要求 storage 与 enqueue：前者写本地目录，后者只记下来（不碰 broker、不跑解析）
    app.state.knowledge_runtime = KnowledgeRuntime(
        sessions=pg["sessions"],
        storage=LocalStorage(tmp_path / "storage"),
        enqueue=lambda name, args: uploads.append((name, args)),
    )
    async with pg["sessions"]() as session:
        _, plain = await ApiKeyService(session, pepper=pepper, redis=redis_it).create(
            pg["app_a"],
            name="guide-e2e",
            scopes=["chat", "kb:write"],
            rpm_limit=1000,
        )
        await session.commit()

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    serving = asyncio.create_task(server.serve())
    for _ in range(500):
        if server.started:
            break
        await asyncio.sleep(0.02)
    else:
        server.should_exit = True
        raise RuntimeError("uvicorn 没有起来")

    try:
        yield {
            "base": f"http://127.0.0.1:{port}",
            "key": plain,
            "app_id": str(pg["app_a"]),
            "uploads": uploads,
        }
    finally:
        server.should_exit = True
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(serving, timeout=10)
        await chat_runtime.drain()


def test_the_guide_still_has_the_sections_under_test() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    for needle in ("## 2. curl", "## 3. Python（httpx", "## 4. Python（OpenAI SDK"):
        assert needle in text, f"指南缺少小节：{needle}"
    for needle in ("## 5. JavaScript", "### 7.4 Python 校验示例"):
        assert needle in text, f"指南缺少小节：{needle}"


async def test_section_2_curl(live: dict[str, Any], upload_file: Path) -> None:
    curl = shutil.which("curl")
    if curl is None:
        pytest.skip("本机没有 curl")

    def run(*args: str) -> tuple[int, str]:
        done = subprocess.run(  # noqa: S603 - 固定参数，没有 shell
            [curl, *args, "-w", "\n%{http_code}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
        body, _, status = done.stdout.rpartition("\n")
        return int(status or 0), body

    base, key, app_id = live["base"], live["key"], live["app_id"]
    auth = ["-H", f"Authorization: Bearer {key}", "-H", "Content-Type: application/json"]
    payload = json.dumps({"question": QUESTION, "user_id": "u_10086"}, ensure_ascii=False)

    status, body = await _in_thread(
        run, "-s", "-X", "POST", _url(base, app_id), *auth, "-d", payload
    )
    assert status == 200, body
    answered = json.loads(body)
    assert answered["answer"] and answered["conversation_id"]

    status, body = await _in_thread(
        run,
        "-N",
        "-X",
        "POST",
        _url(base, app_id),
        *auth,
        "-d",
        json.dumps({"question": QUESTION, "stream": True}, ensure_ascii=False),
    )
    assert status == 200, body
    deltas = re.findall(r"^event: delta$", body, re.M)
    assert deltas, f"没有收到 delta 事件：{body[:200]}"
    assert "event: end" in body

    status, body = await _in_thread(
        run,
        "-s",
        "-X",
        "POST",
        _url(base, app_id, "documents"),
        "-H",
        f"Authorization: Bearer {key}",
        "-H",
        f"Idempotency-Key: {uuid.uuid4()}",
        "-F",
        f"file=@{upload_file};filename=guide.txt",
    )
    assert status in (200, 201, 202), body
    created = json.loads(body)
    assert created.get("document", created).get("id")


async def test_section_3_python_httpx(live: dict[str, Any]) -> None:
    """照抄指南 §3 的写法（同步 httpx），断言拿到文档承诺的字段。"""

    def guide_code(base: str, key: str, app_id: str) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {key}"}

        def ask(question: str, conversation_id: str | None = None) -> dict[str, Any]:
            res = httpx.post(
                _url(base, app_id),
                headers=headers,
                timeout=60,
                json={"question": question, "conversation_id": conversation_id},
            )
            res.raise_for_status()
            return res.json()

        def ask_stream(question: str) -> tuple[str, dict[str, Any] | None]:
            texts: list[str] = []
            end: dict[str, Any] | None = None
            with httpx.stream(
                "POST",
                _url(base, app_id),
                headers=headers,
                timeout=60,
                json={"question": question, "stream": True},
            ) as res:
                res.raise_for_status()
                event = None
                for line in res.iter_lines():
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        data = json.loads(line[5:])
                        if event == "delta":
                            texts.append(data["text"])
                        elif event == "end":
                            end = data
                        elif event == "error":
                            raise RuntimeError(data)
            return "".join(texts), end

        first = ask(QUESTION)
        second = ask(FOLLOW_UP, first["conversation_id"])
        text, end = ask_stream(QUESTION)
        return {"first": first, "second": second, "text": text, "end": end}

    result = await _in_thread(guide_code, live["base"], live["key"], live["app_id"])
    first = result["first"]
    assert first["answer"]
    assert isinstance(first["citations"], list)
    assert result["second"]["conversation_id"] == first["conversation_id"]
    assert result["text"]
    assert result["end"] is not None and result["end"]["answer"]


async def test_section_4_openai_sdk(live: dict[str, Any]) -> None:
    """官方 SDK 零改造：`model` 用 app_id，引用在 `aics` 扩展字段里。

    会话语义（指南 §4 的注）：不带 `X-AICS-Conversation-Id` 的请求是**无状态**轮次，
    服务端为它建的会话在该轮结束即关闭（`close_reason=stateless`，不占会话表）。
    因此首轮返回的会话 ID 只能用于排障；真正能续聊的是「带过头的那一轮」返回的 ID。
    """

    def guide_code(base: str, key: str, app_id: str) -> dict[str, Any]:
        client = OpenAI(base_url=f"{base}/v1", api_key=key, timeout=60, max_retries=0)
        models = [item.id for item in client.models.list()]
        resp = client.chat.completions.create(
            model=app_id, messages=[{"role": "user", "content": QUESTION}]
        )
        aics = resp.model_extra["aics"]
        first_id = aics["conversation_id"]
        # 第一次带会话 ID：那个会话已随无状态轮次关闭，服务端会新建一个并（此后）保留
        resp2 = client.chat.completions.create(
            model=app_id,
            messages=[{"role": "user", "content": FOLLOW_UP}],
            extra_headers={"X-AICS-Conversation-Id": first_id},
        )
        second_id = resp2.model_extra["aics"]["conversation_id"]
        # 第二次带会话 ID：这一次真的续在同一个会话上
        resp3 = client.chat.completions.create(
            model=app_id,
            messages=[{"role": "user", "content": FOLLOW_UP}],
            extra_headers={"X-AICS-Conversation-Id": second_id},
        )
        streamed = "".join(
            chunk.choices[0].delta.content or ""
            for chunk in client.chat.completions.create(
                model=app_id, stream=True, messages=[{"role": "user", "content": QUESTION}]
            )
        )
        return {
            "models": models,
            "content": resp.choices[0].message.content,
            "aics": aics,
            "first_id": first_id,
            "second_id": second_id,
            "third_id": resp3.model_extra["aics"]["conversation_id"],
            "streamed": streamed,
        }

    result = await _in_thread(guide_code, live["base"], live["key"], live["app_id"])
    assert live["app_id"] in result["models"]
    assert result["content"]
    assert isinstance(result["aics"]["citations"], list)
    assert result["second_id"] != result["first_id"]
    assert result["third_id"] == result["second_id"]
    assert result["streamed"]


async def test_section_5_sse_parsing(live: dict[str, Any]) -> None:
    """§5 是 JS，但解析规则是契约：按空行分帧、`event:` 与 `data:` 各一行、心跳没有 data。"""

    def fetch_frames(base: str, key: str, app_id: str) -> list[tuple[str | None, dict[str, Any]]]:
        frames: list[tuple[str | None, dict[str, Any]]] = []
        with httpx.stream(
            "POST",
            _url(base, app_id),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"question": QUESTION, "stream": True},
            timeout=60,
        ) as res:
            res.raise_for_status()
            buf = ""
            for chunk in res.iter_text():
                buf += chunk
                while "\n\n" in buf:
                    raw, buf = buf.split("\n\n", 1)
                    event = re.search(r"^event: (.*)$", raw, re.M)
                    data = re.search(r"^data: (.*)$", raw, re.M)
                    if data is None:  # 心跳
                        continue
                    frames.append((event.group(1) if event else None, json.loads(data.group(1))))
        return frames

    frames = await _in_thread(fetch_frames, live["base"], live["key"], live["app_id"])
    deltas = [obj["text"] for name, obj in frames if name == "delta"]
    ends = [obj for name, obj in frames if name == "end"]
    assert deltas and ends, frames
    assert "".join(deltas) == ends[-1]["answer"]


def test_section_7_4_signature() -> None:
    """指南 §7.4 的验签示例：四头齐全、通过，篡改正文与过期时间戳都失败。"""
    secret = "whsec_" + uuid.uuid4().hex + uuid.uuid4().hex[:11]
    body = json.dumps(
        payloads.build_payload(
            delivery_id=uuid.uuid4(),
            event="handoff.released",
            app_id=uuid.uuid4(),
            conversation_id=uuid.uuid4(),
            user_id="u_1",
            seq=3,
            occurred_at=datetime.now(UTC),
            data=payloads.handoff_released(
                agent_id="ag_1", note=None, released_by="agent", reason="agent"
            ),
        ),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    timestamp = str(int(time.time()))
    signature = signing.sign(secret, timestamp, body)
    headers = signing.headers(
        delivery_id=str(uuid.uuid4()),
        event="handoff.released",
        timestamp=timestamp,
        signature=signature,
    )
    for name in (
        signing.EVENT_HEADER,
        signing.DELIVERY_HEADER,
        signing.TIMESTAMP_HEADER,
        signing.SIGNATURE_HEADER,
    ):
        assert name in headers
    assert verify_like_the_guide(secret, timestamp, body, signature) is True
    assert verify_like_the_guide(secret, timestamp, body + b" ", signature) is False
    stale = str(int(timestamp) - 301)
    assert verify_like_the_guide(secret, stale, body, signing.sign(secret, stale, body)) is False
