"""M2.6 end-to-end through the real HTTP API (uvicorn) with real DeepSeek + DashScope.

    PYTHONPATH=. uv run python -m scripts.e2e_m2.e2e_m26_http [--passes 2] [--max-yuan 1.0]

Builds the M1 knowledge base into the configured PG + Qdrant (document embeddings come from
the shared eval cache, so a rebuild is free), creates an API key, serves `aics.main.create_app()` with uvicorn
on a local socket and drives scripted multi-turn conversations over SSE with httpx. Online LLM /
rerank / query-embedding calls are *live* (that is what the first-token latency measures).

Checks: SSE event order, citations, recall, out-of-scope, refusals → handoff → wait message,
keyword handoff, blocking mode, 401 / 422, a client disconnect mid-stream (interrupted message
persisted), message paging. Cost is computed from the token counts persisted on messages.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import statistics
import time
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import uvicorn
from qdrant_client import AsyncQdrantClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import aics.main
from aics.core.config import get_settings
from aics.db.models import ApiKey, App, Conversation, Message
from aics.modules.apikeys.service import ApiKeyService
from aics.modules.apps.service import AppService
from aics.vectorstore import QdrantVectorStore
from scripts.e2e_m2 import eval_m22
from scripts.e2e_m2.eval_m22 import CachedEmbedder
from scripts.e2e_m2.smoke_m23 import FALLBACK, SCOPES

HERE = Path(__file__).resolve().parent
OUT = HERE / "results" / "e2e_m26_http.json"
APP_NAME = "e2e-m26-http"
PRICE_MISS, PRICE_HIT, PRICE_OUT = 2.0, 0.2, 8.0  # yuan / 1M (DeepSeek, conservative)
FIRST_TOKEN_TARGET_MS = 2500

# (question, expectation) — expectation keys: intent, answer_type, handoff, contains, cites,
# reply_by. Checked loosely: an LLM turn may legitimately vary in wording.
MAIN = [
    ("你好", {"intent": "chitchat", "answer_type": "answer"}),
    (
        "我买了台 X5 空气净化器，订单号 2026101500777，七天无理由退货需要满足什么条件？",
        {"intent": "kb_qa", "answer_type": "answer", "cites": True},
    ),
    ("退款一般多久到账？", {"answer_type": "answer", "cites": True}),
    ("X5 的滤网多久换一次？", {"answer_type": "answer", "cites": True}),
    ("星河豆积分的有效期是多久？", {"answer_type": "answer", "cites": True}),
    ("我刚才给你的订单号是多少？", {"intent": "recall", "contains": "2026101500777"}),
    ("帮我写一首关于秋天的诗", {"intent": "out_of_scope", "answer_type": "refused"}),
    ("从星河优选发货寄到四川成都一般要几天？", {"answer_type": "answer"}),
    ("你们公司的股票代码是多少？", {"answer_type": "refused"}),
    # max_refusals=2: the turn after two consecutive kb refusals hands off (out_of_scope replies
    # do not count), so the exact handoff turn depends on how the model classified the above.
    ("你们老板叫什么名字？", {}),
    ("店铺老板的手机号是多少？", {}),
    ("还在吗？", {"reply_by": "ai"}),  # handoff_ai_mode=continue: AI keeps answering if pending
]
KEYWORD = [
    ("我要转人工", {"handoff": True, "intent": "human"}),
]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def setup(sessions: Any, s: Any, store: Any, classify_timeout_ms: int) -> tuple[UUID, str]:
    async with sessions() as db:
        old = select(App.id).where(App.name == APP_NAME)
        await db.execute(delete(Conversation).where(Conversation.app_id.in_(old)))
        await db.execute(delete(ApiKey).where(ApiKey.app_id.in_(old)))
        await db.commit()
    eval_m22.APP_NAME = APP_NAME
    embedder = CachedEmbedder(eval_m22.build_embedder(s))
    app_id = await eval_m22.build(sessions, store, embedder, s)
    embedder.cache.save()
    async with sessions() as db:
        await AppService(db).update_app(
            app_id,
            {
                "name": "星河优选",
                "description": SCOPES["full"],
                "fallback_text": FALLBACK,
                "rag_config": {"intent": {"classify_timeout_ms": classify_timeout_ms}},
            },
        )
        _, plain = await ApiKeyService(db, pepper=s.api_key_pepper.get_secret_value()).create(
            app_id, name="e2e-m26", scopes=["chat"], rpm_limit=120
        )
        await db.commit()
    return app_id, plain


async def sse_turn(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    *,
    disconnect_after_delta: bool = False,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    events: list[str] = []
    first_ms = None
    end: dict[str, Any] = {}
    start: dict[str, Any] = {}
    cites: list[Any] = []
    async with client.stream("POST", url, headers=headers, json=body | {"stream": True}) as r:
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {(await r.aread())[:300]!r}")
        ev = None
        async for line in r.aiter_lines():
            if line.startswith("event: "):
                ev = line[7:]
                events.append(ev)
            elif line.startswith("data: ") and ev:
                data = json.loads(line[6:])
                if ev == "start":
                    start = data
                elif ev == "citations":
                    cites = data["citations"]
                elif ev == "delta" and first_ms is None:
                    first_ms = int((time.perf_counter() - t0) * 1000)
                    if disconnect_after_delta:
                        break
                elif ev == "end":
                    end = data
                elif ev == "error":
                    end = {"error": data}
    return {
        "events": events,
        "start": start,
        "end": end,
        "citations": cites,
        "client_first_token_ms": first_ms,
        "wall_ms": int((time.perf_counter() - t0) * 1000),
    }


def order_ok(events: list[str]) -> bool:
    """start → trace? → citations? → delta* → end."""
    rank = {"start": 0, "trace": 1, "citations": 2, "delta": 3, "end": 4}
    seq = [rank[e] for e in events if e in rank]
    return bool(events) and events[0] == "start" and events[-1] == "end" and seq == sorted(seq)


def check(exp: dict[str, Any], t: dict[str, Any]) -> list[str]:
    end, bad = t["end"], []
    if "error" in end:
        return [f"error {end['error']}"]
    if not order_ok(t["events"]):
        bad.append(f"order {t['events']}")
    for k in ("answer_type", "handoff", "reply_by", "handoff_status"):
        if k in exp and end.get(k) != exp[k]:
            bad.append(f"{k}={end.get(k)}")
    if "intent" in exp and (end.get("intent") or {}).get("name") != exp["intent"]:
        bad.append(f"intent={(end.get('intent') or {}).get('name')}")
    if exp.get("cites") and not end.get("used_citations"):
        bad.append("no citation")
    if "contains" in exp and exp["contains"] not in (end.get("answer") or ""):
        bad.append("missing value")
    return bad


async def run(args: argparse.Namespace) -> None:
    s = get_settings()
    engine = create_async_engine(s.db_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    # The vectors live in an in-process Qdrant (the shared Qdrant needs credentials this box
    # does not hold), so the API server runs in-process too: real uvicorn on a real socket,
    # wired by aics.main exactly as in production except for the vector store instance.
    store = QdrantVectorStore(
        AsyncQdrantClient(location=":memory:"),
        prefix="aics_e2e26_",
        embed_model=s.embed_model,
        dim=s.embed_dim,
    )
    app_id, key = await setup(sessions, s, store, args.classify_timeout_ms)
    aics.main.get_vector_store = lambda: store  # type: ignore[assignment]
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(aics.main.create_app(), port=port, access_log=False, log_level="warning")
    )
    serve = asyncio.create_task(server.serve())
    base = f"http://127.0.0.1:{port}"
    url = f"{base}/api/v1/apps/{app_id}/chat"
    auth = {"Authorization": f"Bearer {key}"}
    report: dict[str, Any] = {"turns": [], "checks": {}}
    t_start = time.time()
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            for _ in range(150):
                if serve.done():
                    raise RuntimeError("uvicorn exited")
                try:
                    if (await client.get(f"{base}/healthz")).status_code < 500:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.2)
            r = await client.post(url, json={"question": "hi"})
            report["checks"]["no_key_401"] = r.status_code == 401
            r = await client.post(
                url, json={"question": "hi"}, headers={"Authorization": "Bearer sk-bad"}
            )
            report["checks"]["bad_key_401"] = r.status_code == 401
            r = await client.post(url, json={"question": ""}, headers=auth)
            report["checks"]["blank_422"] = r.status_code == 422

            convs: list[str] = []
            for p in range(args.passes):
                for script_name, script in (("main", MAIN), ("keyword", KEYWORD)):
                    cid = None
                    for i, (q, exp) in enumerate(script):
                        body = {"question": q, "user_id": f"e2e-{p}"}
                        if cid:
                            body["conversation_id"] = cid
                        t = await sse_turn(client, url, auth, body)
                        cid = t["start"].get("conversation_id") or cid
                        bad = check(exp, t)
                        end = t["end"]
                        report["turns"].append(
                            {
                                "pass": p,
                                "script": script_name,
                                "turn": i,
                                "q": q,
                                "intent": (end.get("intent") or {}).get("name"),
                                "answer_type": end.get("answer_type"),
                                "reply_by": end.get("reply_by"),
                                "handoff": end.get("handoff"),
                                "answer": end.get("answer"),
                                "used_citations": end.get("used_citations"),
                                "client_first_token_ms": t["client_first_token_ms"],
                                "latency_ms": end.get("latency_ms"),
                                "events": t["events"],
                                "fail": bad,
                            }
                        )
                        print(
                            p,
                            script_name,
                            i,
                            "OK " if not bad else f"BAD {bad}",
                            q[:30],
                            "|",
                            (end.get("answer") or "")[:60],
                            t["client_first_token_ms"],
                        )
                    convs.append(cid)

            # blocking mode
            r = await client.post(url, json={"question": "发票在哪里开？"}, headers=auth)
            b = r.json()
            report["checks"]["blocking_ok"] = r.status_code == 200 and bool(b.get("answer"))
            report["checks"]["ratelimit_headers"] = "x-ratelimit-limit" in r.headers
            # disconnect mid-stream → interrupted assistant message persisted
            t = await sse_turn(
                client,
                url,
                auth,
                {"question": "请详细说明七天无理由退货的条件、流程、运费承担和退款到账时间。"},
                disconnect_after_delta=True,
            )
            dcid = t["start"]["conversation_id"]
            await asyncio.sleep(3)
            r = await client.get(
                f"{base}/api/v1/apps/{app_id}/conversations/{dcid}/messages", headers=auth
            )
            msgs = r.json()["items"] if r.status_code == 200 else []
            bot = [m for m in msgs if m["role"] == "assistant"]
            report["checks"]["disconnect_persisted"] = bool(bot) and len(msgs) == 2
            report["checks"]["disconnect_message"] = bot[0] if bot else None
            # paging over the main conversation
            r = await client.get(
                f"{base}/api/v1/apps/{app_id}/conversations/{convs[0]}/messages?limit=5",
                headers=auth,
            )
            page = r.json()
            report["checks"]["paging"] = (
                r.status_code == 200 and len(page["items"]) == 5 and bool(page.get("next_cursor"))
            )
    finally:
        server.should_exit = True
        await serve

    async with sessions() as db:
        row = (
            await db.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(Message.prompt_tokens), 0),
                    func.coalesce(func.sum(Message.completion_tokens), 0),
                ).where(
                    Message.app_id == app_id,
                    Message.role == "assistant",
                    Message.created_at >= func.to_timestamp(t_start),
                )
            )
        ).one()
        server_ft = [
            r
            for (r,) in (
                await db.execute(
                    select(Message.first_token_ms).where(
                        Message.app_id == app_id,
                        Message.first_token_ms.is_not(None),
                        Message.created_at >= func.to_timestamp(t_start),
                    )
                )
            ).all()
        ]
    await engine.dispose()
    _, pt, ct = row
    llm_turns = [
        t
        for t in report["turns"]
        if t["client_first_token_ms"] is not None
        and t["reply_by"] == "ai"
        and t["intent"] != "chitchat"
    ]
    ft = sorted(t["client_first_token_ms"] for t in llm_turns)

    def pct(xs: list[int], q: float) -> int | None:
        return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None

    report["classify_timeout_ms"] = args.classify_timeout_ms
    report["latency"] = {
        "client_first_token_ms": ft,
        "p50": int(statistics.median(ft)) if ft else None,
        "p90": pct(ft, 0.9),
        "server_first_token_ms_p50": int(statistics.median(server_ft)) if server_ft else None,
        "target_ms": FIRST_TOKEN_TARGET_MS,
    }
    report["cost"] = {
        "prompt_tokens": int(pt),
        "completion_tokens": int(ct),
        "yuan_conservative": round((pt * PRICE_MISS + ct * PRICE_OUT) / 1e6, 4),
        "note": "LLM only (excl. DeepSeek cache discount); query embedding + rerank ≈ 0.002 元/轮",
    }
    report["summary"] = {
        "turns": len(report["turns"]),
        "failed": [
            (t["pass"], t["script"], t["turn"], t["fail"]) for t in report["turns"] if t["fail"]
        ],
        "checks_ok": all(v for k, v in report["checks"].items() if k != "disconnect_message"),
    }
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1))  # noqa: ASYNC240
    print(
        json.dumps(
            {k: report[k] for k in ("latency", "cost", "summary", "checks")},
            ensure_ascii=False,
            indent=1,
        )
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--passes", type=int, default=2)
    ap.add_argument("--classify-timeout-ms", type=int, default=2000)
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
