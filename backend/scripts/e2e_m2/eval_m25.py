"""M2.5 long-conversation memory evaluation and history_token_budget comparison
(对话上下文管理 §12.2, 设计讨论 0002 C1).

Replays `memory_cases.json` (11 conversations, 24–60 turns) through the production path in the
same order chat.service will use (M2.6):
  user message persisted → run_rag(conversation_id, user_message_id, MemoryPort) →
  assistant message persisted → memory.capture_slots → memory.maybe_compact(run_mode=eval).
Filler turns are written as-is (fixed history) and also go through maybe_compact.
Each budget gets fresh conversations; compact_trigger_tokens = 2 × budget (§12.2).

Real DeepSeek (thinking off) + DashScope; LLM / rerank / embedding responses are cached, so a
re-run is free. Cost = new calls only, reported with DeepSeek's prompt-cache hits split out.

Usage (from backend/, env loaded):
  uv run python -m scripts.e2e_m2.eval_m25 [--budgets 3000,8000,16000] [--only c01,c02]
                                            [--max-yuan 2.8]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

from qdrant_client import AsyncQdrantClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from aics.core.config import get_settings
from aics.db.models import App, Conversation
from aics.memory.compactor import Compactor, CompactParams
from aics.memory.locks import LocalLock
from aics.memory.service import MemoryService
from aics.memory.units import pg_units
from aics.modules.chat.memory_port import MemoryPortAdapter
from aics.modules.chat.repository import ConversationRepository, MessageRepository
from aics.modules.chat.schemas import NewConversation, NewMessage
from aics.modules.knowledge.chunk_search import PgChunkSearch
from aics.providers import build_embedder, build_llm, build_reranker
from aics.rag.config import AppRagConfig
from aics.rag.graph import run_rag_collect
from aics.rag.identifiers import find_identifiers
from aics.rag.ports import RagPorts
from aics.rag.retrieval import EmbeddingVectorSearch
from aics.rag.schemas import AppProfile, RagInput, RunMode
from aics.vectorstore import QdrantVectorStore
from scripts.e2e_m2.eval_m22 import (
    APP_NAME,
    EMBED_PRICE,
    RERANK_PRICE,
    RES,
    CachedEmbedder,
    CachedReranker,
    _JsonCache,
    build,
)
from scripts.e2e_m2.smoke_m23 import FALLBACK, SCOPES, CachedLLM

CASES = Path(__file__).with_name("memory_cases.json")
PRICE_MISS, PRICE_HIT, PRICE_OUT = 2.0, 0.2, 8.0  # yuan / 1M (conservative output price)


def cost_of(entries: list[dict[str, Any]]) -> dict[str, float]:
    p = c = hit = 0
    for e in entries:
        u = e.get("usage") or {}
        p += int(u.get("input_tokens") or 0)
        c += int(u.get("output_tokens") or 0)
        hit += int((u.get("input_token_details") or {}).get("cache_read") or 0)
    conservative = (p * PRICE_MISS + c * PRICE_OUT) / 1e6
    actual = ((p - hit) * PRICE_MISS + hit * PRICE_HIT + c * PRICE_OUT) / 1e6
    return {
        "calls": len(entries),
        "prompt_tokens": p,
        "cache_hit_tokens": hit,
        "completion_tokens": c,
        "yuan_conservative": round(conservative, 4),
        "yuan_with_cache": round(actual, 4),
    }


def check_turn(spec: dict[str, Any], res: Any, facts: dict[str, str]) -> dict[str, Any]:
    r, a = res.rewritten_query or "", res.answer or ""
    out: dict[str, Any] = {}
    if spec.get("rewrite_any"):
        out["rewrite_ok"] = any(s in r for s in spec["rewrite_any"])
    if spec.get("answer_any"):
        out["answer_ok"] = res.answer_type == "answer" and any(s in a for s in spec["answer_any"])
    if spec.get("forbid"):
        out["forbid_hit"] = [s for s in spec["forbid"] if s in r or s in a]
    if "facts" in spec:
        # None = must be absent; otherwise the stored (normalized) value must contain the
        # expected core token (e.g. product "X5" ⊂ "星河 X5 空气净化器").
        out["facts_ok"] = all(
            (facts.get(k) is None) if v is None else (v in (facts.get(k) or ""))
            for k, v in spec["facts"].items()
        )
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--budgets", default="3000,8000,16000")
    ap.add_argument("--only", default="")
    ap.add_argument("--max-yuan", type=float, default=2.8)
    args = ap.parse_args()
    s = get_settings()
    engine = create_async_engine(s.db_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    client = AsyncQdrantClient(location=":memory:")
    store = QdrantVectorStore(
        client, prefix="aics_eval_", embed_model=s.embed_model, dim=s.embed_dim
    )
    embedder = CachedEmbedder(build_embedder(s))
    reranker = CachedReranker(build_reranker(s))
    llm = CachedLLM(build_llm(s))
    llm.cache = _JsonCache("llm_m25_cache.json")
    async with sessions() as ss:  # build() recreates the app; drop the previous run's chats
        old = select(App.id).where(App.name == APP_NAME)
        await ss.execute(delete(Conversation).where(Conversation.app_id.in_(old)))
        await ss.commit()
    app_id = await build(sessions, store, embedder, s)
    units = pg_units(sessions)
    compactor = Compactor(units, llm, LocalLock())
    memory = MemoryService(units, compactor=compactor)
    ports = RagPorts(
        chunk_search=PgChunkSearch(sessions),
        vector=EmbeddingVectorSearch(embedder, store),
        reranker=reranker,
        llm=llm,
        memory=MemoryPortAdapter(memory),
    )
    profile = AppProfile(name="星河优选", description=SCOPES["full"], fallback_text=FALLBACK)
    convs = json.loads(CASES.read_text())["conversations"]
    if args.only:
        keep = set(args.only.split(","))
        convs = [c for c in convs if c["id"] in keep]
    all_ids = {c["id"]: {v for t in c["turns"] for _, v in find_identifiers(t["q"])} for c in convs}
    report: dict[str, Any] = {"conversations": len(convs), "budgets": {}}
    out_path = RES / f"memory_eval_m25{'_' + args.only.replace(',', '_') if args.only else ''}.json"
    for budget in [int(b) for b in args.budgets.split(",")]:
        cfg = AppRagConfig(history_token_budget=budget, compact_trigger_tokens=2 * budget)
        params = CompactParams(
            keep_recent_turns=cfg.keep_recent_turns,
            compact_trigger_tokens=cfg.compact_trigger_tokens,
            enable_facts=cfg.enable_facts,
            model=cfg.summary_model or cfg.llm_model,
        )
        before = set(llm.cache.data)
        rr0, t0 = reranker.tokens, time.perf_counter()
        rows: list[dict[str, Any]] = []
        compactions: Counter[str] = Counter()
        conv_summ: list[dict[str, Any]] = []
        for conv in convs:
            async with sessions() as ss:
                cid = (
                    await ConversationRepository(ss).create(app_id, NewConversation(channel="eval"))
                ).id
                await ss.commit()
            for ti, turn in enumerate(conv["turns"]):
                spent = cost_of([llm.cache.data[k] for k in set(llm.cache.data) - before])
                if spent["yuan_conservative"] > args.max_yuan:
                    raise SystemExit(f"budget guard: {spent} > {args.max_yuan}")
                async with sessions() as ss:
                    msgs = MessageRepository(ss)
                    um = await msgs.append(app_id, cid, NewMessage(role="user", content=turn["q"]))
                    if turn["t"] == "f":
                        await msgs.append(
                            app_id,
                            cid,
                            NewMessage(
                                role="assistant", content=turn["a"], answer_type=turn["type"]
                            ),
                        )
                    await ss.commit()
                flags: list[str] = []
                if turn["t"] == "run":
                    res, _ = await run_rag_collect(
                        RagInput(
                            app_id=app_id,
                            conversation_id=cid,
                            user_message_id=um.id,
                            question=turn["q"],
                            profile=profile,
                            config=cfg,
                            run_mode=RunMode.eval,
                        ),
                        ports,
                    )
                    async with sessions() as ss:
                        await MessageRepository(ss).append(
                            app_id,
                            cid,
                            NewMessage(
                                role="assistant",
                                content=res.answer,
                                answer_type=res.answer_type,
                                rewritten_query=res.rewritten_query,
                                degraded=res.degraded,
                            ),
                        )
                        await ss.commit()
                    await memory.capture_slots(
                        app_id,
                        cid,
                        res.slots,
                        intent_source=res.intent.source,
                        intent_confidence=res.intent.confidence,
                        user_message_id=um.id,
                        enable_facts=cfg.enable_facts,
                    )
                    flags = res.degraded
                trig, cres = await memory.maybe_compact(
                    app_id, cid, params, run_mode="eval", flags=flags
                )
                if cres is not None:
                    compactions[cres.result] += 1
                if turn["t"] != "run":
                    continue
                async with units() as u:
                    facts = {f.key: f.value for f in await u.store.list_facts(app_id, cid)}
                chk = check_turn(turn, res, facts)
                foreign = [
                    v
                    for oid, vals in all_ids.items()
                    if oid != conv["id"]
                    for v in vals
                    if v in (res.answer + (res.rewritten_query or ""))
                ]
                ctx = res.trace.get("context", {})
                usage = res.context_usage
                rows.append(
                    {
                        "conv": conv["id"],
                        "turn": ti,
                        "q": turn["q"],
                        "tags": turn.get("tags", []),
                        "rewritten": res.rewritten_query,
                        "intent": res.intent.name,
                        "answer_type": res.answer_type,
                        "answer": res.answer[:200],
                        "slots": res.slots,
                        "facts": facts,
                        "degraded": res.degraded,
                        "context_usage": usage,
                        "summary_version": ctx.get("summary_version"),
                        "recent_turns": ctx.get("recent_turns"),
                        "prompt_tokens": res.usage.prompt_tokens,
                        "foreign_ids": foreign,
                        **chk,
                    }
                )
                print(
                    budget,
                    conv["id"],
                    ti,
                    res.intent.name,
                    res.answer_type,
                    {k: v for k, v in chk.items()},
                    (res.rewritten_query or "")[:40],
                )
            async with units() as u:
                st = u.store
                hist = await st.list_fact_history(app_id, cid)
                state = await st.get_summary_state(app_id, cid)
            conv_summ.append(
                {
                    "conv": conv["id"],
                    "summary_version": state.version,
                    "summary": state.summary.model_dump() if state.summary else None,
                    "fact_history": [
                        (h.key, h.op, h.old_value, h.new_value, getattr(h, "source", None))
                        for h in hist
                    ],
                }
            )
        new = [llm.cache.data[k] for k in set(llm.cache.data) - before]
        report["budgets"][str(budget)] = {
            "metrics": aggregate(rows),
            "compactions": dict(compactions),
            "cost": {
                "llm": cost_of(new),
                "rerank_yuan": round((reranker.tokens - rr0) * RERANK_PRICE / 1e6, 4),
            },
            "live_first_token_ms": sorted(
                e["first_token_ms"] for e in new if e.get("first_token_ms") is not None
            ),
            "wall_s": round(time.perf_counter() - t0, 1),
            "conversations": conv_summ,
            "rows": rows,
        }
        print(budget, json.dumps(report["budgets"][str(budget)]["metrics"], ensure_ascii=False))
        print(budget, report["budgets"][str(budget)]["cost"], dict(compactions))
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    report["embed_yuan"] = round(embedder.tokens * EMBED_PRICE / 1e6, 4)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    await client.close()
    await engine.dispose()


def _rate(xs: list[bool]) -> float | None:
    return round(sum(xs) / len(xs), 3) if xs else None


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def tagged(tag: str) -> list[dict[str, Any]]:
        return [r for r in rows if tag in r["tags"]]

    def passed(r: dict[str, Any]) -> bool:
        return (
            r.get("rewrite_ok", True)
            and r.get("answer_ok", True)
            and not r.get("forbid_hit")
            and r.get("facts_ok", True)
        )

    confused = [bool(r.get("forbid_hit") or r["foreign_ids"]) for r in rows]
    return {
        "run_turns": len(rows),
        "retention": _rate([passed(r) for r in tagged("recall")]),
        "update_correctness": _rate([passed(r) for r in tagged("update") + tagged("retract")]),
        "confusion_rate": _rate(confused),
        "rewrite_correctness": _rate([r["rewrite_ok"] for r in rows if "rewrite_ok" in r]),
        "answer_quality": _rate([r["answer_ok"] for r in rows if "answer_ok" in r]),
        "fact_accuracy": _rate([r["facts_ok"] for r in rows if "facts_ok" in r]),
        "trap_pass": _rate([passed(r) for r in tagged("trap")]),
        "identifier_mismatch_flags": sum("identifier_mismatch" in r["degraded"] for r in rows),
        "history_gap_turns": sum("history_gap" in r["degraded"] for r in rows),
        "avg_prompt_tokens": round(sum(r["prompt_tokens"] for r in rows) / max(1, len(rows))),
        "avg_history_tokens": round(
            sum(
                sum(r["context_usage"].get(k, 0) for k in ("facts", "summary", "recent"))
                for r in rows
            )
            / max(1, len(rows))
        ),
    }


if __name__ == "__main__":
    asyncio.run(main())
