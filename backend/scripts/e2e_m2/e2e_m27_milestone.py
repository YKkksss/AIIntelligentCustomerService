"""M2 milestone acceptance over the real HTTP API (M2.7 ③).

    PYTHONPATH=. uv run python -m scripts.e2e_m2.e2e_m27_milestone

The 20 sample questions of the M2.3 smoke test (12 answerable + 8 unanswerable, from
scripts/e2e_m1/questions.json) are asked through `POST /chat` (SSE), each in its own
conversation, against the sample knowledge base, with real DeepSeek (thinking off) +
DashScope. Answers, citations and an automatic evidence hint are written to
results/e2e_m27_milestone.json for the manual review recorded in the test report.
Then the lifecycle is checked end to end: an idle sweep (clock injected) adds `idle_notice`
and `idle_closed` to one conversation, `GET .../messages` shows them, and the next message
with the old id starts a new conversation (`previous_conversation_id`).
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from qdrant_client import AsyncQdrantClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import aics.main
from aics.core.config import get_settings
from aics.db.models import Message
from aics.modules.chat import lifecycle
from aics.modules.chat.repository import ConversationRepository
from aics.vectorstore import QdrantVectorStore
from scripts.e2e_m2.e2e_m26_http import PRICE_MISS, PRICE_OUT, free_port, setup, sse_turn
from scripts.e2e_m2.smoke_m23 import M1, pick

OUT = Path(__file__).resolve().parent / "results" / "e2e_m27_milestone.json"
M = timedelta(minutes=1)


async def main() -> None:
    s = get_settings()
    engine = create_async_engine(s.db_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    store = QdrantVectorStore(
        AsyncQdrantClient(location=":memory:"),
        prefix="aics_e2e27_",
        embed_model=s.embed_model,
        dim=s.embed_dim,
    )
    app_id, key = await setup(sessions, s, store, 2000)
    aics.main.get_vector_store = lambda: store  # type: ignore[assignment]
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(aics.main.create_app(), port=port, access_log=False, log_level="warning")
    )
    serve = asyncio.create_task(server.serve())
    base = f"http://127.0.0.1:{port}"
    url = f"{base}/api/v1/apps/{app_id}/chat"
    auth = {"Authorization": f"Bearer {key}"}
    questions = pick(json.loads((M1 / "questions.json").read_text()))  # noqa: ASYNC230
    rows: list[dict[str, Any]] = []
    checks: dict[str, Any] = {}
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
            for q in questions:
                t = await sse_turn(client, url, auth, {"question": q["question"]})
                end = t["end"]
                answer = end.get("answer") or ""
                cited = [
                    c for c in t["citations"] if c.get("n") in (end.get("used_citations") or [])
                ]
                hay = answer + "".join(c.get("snippet") or "" for c in cited)
                rows.append(
                    {
                        "id": q["id"],
                        "question": q["question"],
                        "answerable": bool(q["docs"]),
                        "answer_type": end.get("answer_type"),
                        "intent": (end.get("intent") or {}).get("name"),
                        "answer": answer,
                        "cited_titles": [c.get("document_title") for c in cited],
                        "expected_docs": q["docs"],
                        "evidence_hint": any(e in hay for e in q.get("evidence") or []),
                        "doc_hit": any(
                            (c.get("document_title") or "").startswith(tuple(q["docs"]))
                            for c in cited
                        )
                        if q["docs"]
                        else None,
                        "events": t["events"],
                        "client_first_token_ms": t["client_first_token_ms"],
                        "conversation_id": t["start"].get("conversation_id"),
                    }
                )
                r = rows[-1]
                print(r["id"], r["answer_type"], r["doc_hit"], r["evidence_hint"], answer[:70])

            # lifecycle through the API: notice + close by the sweep, visible in messages
            cid = rows[0]["conversation_id"]
            async with sessions() as ss:
                c = await ConversationRepository(ss).get_or_404(app_id, cid)
            await lifecycle.sweep(sessions, now=c.last_activity_at + 9 * M)
            await lifecycle.sweep(sessions, now=c.last_activity_at + 10 * M)
            r = await client.get(
                f"{base}/api/v1/apps/{app_id}/conversations/{cid}/messages", headers=auth
            )
            body = r.json()
            checks["messages_kinds"] = [(m["role"], m["kind"]) for m in body["items"]]
            checks["conversation_status"] = body["conversation"]["status"]
            t = await sse_turn(
                client, url, auth, {"question": "退款多久到账？", "conversation_id": cid}
            )
            checks["previous_conversation_id_ok"] = (
                t["end"].get("previous_conversation_id") == cid
                and t["start"].get("conversation_id") != cid
            )
    finally:
        server.should_exit = True
        await serve
    async with sessions() as ss:
        pt, ct = (
            await ss.execute(
                select(
                    func.coalesce(func.sum(Message.prompt_tokens), 0),
                    func.coalesce(func.sum(Message.completion_tokens), 0),
                ).where(
                    Message.app_id == app_id,
                    Message.created_at >= func.to_timestamp(t_start),
                )
            )
        ).one()
    await engine.dispose()
    ans = [r for r in rows if r["answerable"]]
    una = [r for r in rows if not r["answerable"]]
    summary = {
        "answerable_answered": sum(r["answer_type"] == "answer" for r in ans),
        "answerable_doc_hit": sum(bool(r["doc_hit"]) for r in ans),
        "answerable_evidence_hint": sum(r["evidence_hint"] for r in ans),
        "unanswerable_refused": sum(r["answer_type"] in ("refused", "handoff") for r in una),
        "n_answerable": len(ans),
        "n_unanswerable": len(una),
        "yuan_conservative": round((pt * PRICE_MISS + ct * PRICE_OUT) / 1e6, 4),
        "prompt_tokens": int(pt),
        "completion_tokens": int(ct),
    }
    OUT.write_text(  # noqa: ASYNC240
        json.dumps(
            {"summary": summary, "checks": checks, "rows": rows}, ensure_ascii=False, indent=1
        )
    )
    print(json.dumps({"summary": summary, "checks": checks}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
