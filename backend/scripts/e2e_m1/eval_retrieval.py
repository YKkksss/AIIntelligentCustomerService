"""M1 检索质量评测：向量 / 全文 / 混合(RRF) / 混合+重排，对比上下文前缀与父子切块。

按 RAG 设计文档参数：vector_top_k=20、keyword_top_k=20、RRF k=60、fuse_top_k=30、rerank_top_n=5。
重排直接调用 DashScope text-rerank HTTP 接口（M2.2 才在 src 内实现 provider），结果按 (问题, 文档) 缓存到 results/rerank_pair_cache.json（只为新文本付费）。
用法见 README 段：`--apps apps_r2.json --variants main,mainlegal --out retrieval_eval_r2.json`。
用法：先加载 env（含 AICS_EMBED_API_KEY / AICS_RERANK_API_KEY），再 `uv run python scripts/e2e_m1/eval_retrieval.py`。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import statistics
from pathlib import Path
from uuid import UUID

import httpx
from sqlalchemy import text as sql
from sqlalchemy.ext.asyncio import create_async_engine

from aics.core.config import get_settings
from aics.core.textsearch import build_tsquery
from aics.core.tokens import count_tokens
from aics.providers import build_embedder
from aics.vectorstore import build_vector_store

HERE = Path(__file__).parent
RES = HERE / "results"
VEC_K, KW_K, RRF_K, FUSE_K, TOP_N = 20, 20, 60, 30, 5
EMBED_PRICE, RERANK_PRICE = 0.125, 0.5  # 元 / 百万 token（AICS_MODELS 注册表）

RETRIEVABLE = """
  c.enabled AND c.review_status <> 'flagged' AND c.index_version = d.index_version
  AND d.status <> 'deleting'"""


async def chunk_rows(conn, ids):
    if not ids:
        return {}
    rows = await conn.execute(
        sql(f"""select c.id, c.parent_id, coalesce(c.heading_path,'') hp, c.text, d.title
                from chunks c join documents d on d.id=c.document_id
                where c.id = any(:ids) and {RETRIEVABLE}"""),
        {"ids": list(ids)},
    )
    return {r.id: r for r in rows}


async def fulltext(conn, app_id, q, custom_dict):
    tsq = build_tsquery(app_id, q, custom_dict)
    if not tsq:
        return []
    rows = await conn.execute(
        sql(f"""select c.id, ts_rank_cd(c.tsv, to_tsquery('simple', :tsq)) s
                from chunks c join documents d on d.id=c.document_id
                where c.app_id=:app and c.tsv @@ to_tsquery('simple', :tsq) and {RETRIEVABLE}
                order by s desc limit :k"""),
        {"tsq": tsq, "app": app_id, "k": KW_K},
    )
    return [(r.id, float(r.s)) for r in rows]


def rrf(*lists):
    score: dict = {}
    for lst in lists:
        for rank, cid in enumerate(lst, 1):
            score[cid] = score.get(cid, 0.0) + 1.0 / (RRF_K + rank)
    return [c for c, _ in sorted(score.items(), key=lambda x: -x[1])]


class Reranker:
    """交叉编码重排：每个 (问题, 文档) 的分数与同批其他文档无关，按对缓存，只为新文本付费。"""

    def __init__(self, s):
        self.s = s
        self.pair_path = RES / "rerank_pair_cache.json"
        self.pairs: dict[str, float] = (
            json.loads(self.pair_path.read_text()) if self.pair_path.exists() else {}
        )
        self.tokens = 0
        self.calls = 0
        self.client = httpx.AsyncClient(timeout=60)

    def _key(self, query: str, doc: str) -> str:
        return hashlib.sha256(json.dumps([self.s.rerank_model, query, doc]).encode()).hexdigest()

    async def scores(self, query: str, docs: list[str]) -> list[float]:
        if not docs:
            return []
        missing = [d for d in dict.fromkeys(docs) if self._key(query, d) not in self.pairs]
        if missing:
            r = await self.client.post(
                self.s.rerank_url,
                headers={"Authorization": f"Bearer {self.s.rerank_api_key.get_secret_value()}"},
                json={
                    "model": self.s.rerank_model,
                    "input": {"query": query, "documents": missing},
                    "parameters": {"return_documents": False, "top_n": len(missing)},
                },
            )
            r.raise_for_status()
            body = r.json()
            for it in body["output"]["results"]:
                self.pairs[self._key(query, missing[it["index"]])] = float(it["relevance_score"])
            self.tokens += body["usage"]["total_tokens"]
            self.calls += 1
        return [self.pairs[self._key(query, d)] for d in docs]

    def save(self):
        self.pair_path.write_text(json.dumps(self.pairs))


def judge(q, unit):
    """unit = (title, haystack)；命中 = 来源文档在期望列表中且包含任一证据片段。"""
    title, hay = unit
    return any(title.startswith(d) for d in q["docs"]) and any(e in hay for e in q["evidence"])


def first_hit(q, units):
    for i, u in enumerate(units, 1):
        if judge(q, u):
            return i
    return None


def metrics(ranks):
    n = len(ranks)
    return {
        "n": n,
        "hit@1": sum(1 for r in ranks if r and r <= 1) / n,
        "hit@3": sum(1 for r in ranks if r and r <= 3) / n,
        "hit@5": sum(1 for r in ranks if r and r <= 5) / n,
        "mrr": sum(1 / r for r in ranks if r) / n,
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apps", default="apps.json")
    ap.add_argument("--variants", default="main,noprefix,pc")
    ap.add_argument("--out", default="retrieval_eval.json")
    ap.add_argument("--rerank", default=None, help="只对这些变体重排（逗号分隔，缺省全部）")
    args = ap.parse_args()
    s = get_settings()
    apps = json.loads((RES / args.apps).read_text())
    qs = json.loads((HERE / "questions.json").read_text())
    engine = create_async_engine(
        s.db_url.get_secret_value() if hasattr(s.db_url, "get_secret_value") else s.db_url
    )
    store = build_vector_store(s)
    embedder = build_embedder(s)
    rr = Reranker(s)

    qvec_cache = RES / "qvec_cache.json"
    qvecs = json.loads(qvec_cache.read_text()) if qvec_cache.exists() else {}
    embed_tokens = 0
    for q in qs:
        if q["question"] not in qvecs:
            qvecs[q["question"]] = await embedder.embed_query(q["question"])
            embed_tokens += count_tokens(q["question"])
    qvec_cache.write_text(json.dumps(qvecs))

    labels = {
        "main": "flat+前缀",
        "noprefix": "flat 无前缀",
        "pc": "父子+前缀",
        "mainlegal": "flat+前缀+02 文档级 legal",
    }
    variants = [(k, labels.get(k, k)) for k in args.variants.split(",")]
    rerank_set = set((args.rerank or args.variants).split(","))
    report: dict = {"variants": {}, "unanswerable": {}, "failures": {}, "evidence_check": {}}
    async with engine.connect() as conn:
        # 证据校验：每个可回答问题在 main 中至少有一个块命中
        for q in qs:
            if not q["docs"]:
                continue
            rows = await conn.execute(
                sql("""select coalesce(c.heading_path,'') hp, c.text, d.title from chunks c
                       join documents d on d.id=c.document_id where c.app_id=:a
                       and c.index_version=d.index_version"""),
                {"a": apps["main"]},
            )
            n = sum(1 for r in rows if judge(q, (r.title, r.hp + "\n" + r.text)))
            report["evidence_check"][q["id"]] = n
        for key, label in variants:
            app_id = UUID(apps[key])
            cd = list(
                (
                    await conn.execute(
                        sql("select custom_dict from apps where id=:a"), {"a": app_id}
                    )
                ).scalar()
                or []
            )
            ranks = {m: [] for m in ("vector", "fulltext", "hybrid", "hybrid+rerank")}
            top1_ans, unans = [], []
            fails = []
            for q in qs:
                vh = await store.search_chunks(app_id, qvecs[q["question"]], VEC_K)
                vrows = await chunk_rows(conn, [h.chunk_id for h in vh])
                vec = [h.chunk_id for h in vh if h.chunk_id in vrows]
                vscore = {h.chunk_id: h.score for h in vh}
                kw = [c for c, _ in await fulltext(conn, app_id, q["question"], cd)]
                fused = rrf(vec, kw)[:FUSE_K]
                rows = {**vrows, **await chunk_rows(conn, set(kw) - set(vrows))}
                parents = {}
                if key == "pc":
                    pids = {rows[c].parent_id for c in rows if rows[c].parent_id}
                    if pids:
                        pr = await conn.execute(
                            sql(
                                "select id, coalesce(heading_path,'') hp, text from chunk_parents where id = any(:i)"
                            ),
                            {"i": list(pids)},
                        )
                        parents = {r.id: r for r in pr}

                def unit_of(cid):
                    r = rows[cid]
                    if key == "pc" and r.parent_id in parents:
                        p = parents[r.parent_id]
                        return (r.parent_id, r.title, p.hp + "\n" + p.text)
                    return (cid, r.title, r.hp + "\n" + r.text)

                def dedup(cids):
                    seen, out = set(), []
                    for c in cids:
                        uid, t, h = unit_of(c)
                        if uid not in seen:
                            seen.add(uid)
                            out.append((t, h))
                    return out

                cand = dedup(fused)
                if key in rerank_set:
                    sc = await rr.scores(q["question"], [h for _, h in cand])
                else:  # 不重排：保持 RRF 顺序，不计 hybrid+rerank 指标
                    sc = [-float(i) for i in range(len(cand))]
                order = sorted(range(len(cand)), key=lambda i: -sc[i])
                reranked = [cand[i] for i in order]
                top_score = sc[order[0]] if order else 0.0
                maxv = max((vscore[c] for c in vec), default=0.0)
                if not q["docs"]:
                    unans.append({"id": q["id"], "rerank_max": top_score, "vector_max": maxv})
                    continue
                r_ = {
                    "vector": first_hit(q, dedup(vec)),
                    "fulltext": first_hit(q, dedup(kw)),
                    "hybrid": first_hit(q, cand),
                    "hybrid+rerank": first_hit(q, reranked),
                }
                for m, v in r_.items():
                    ranks[m].append(v)
                report.setdefault("per_question", {}).setdefault(label, {})[q["id"]] = r_
                top1_ans.append(
                    {
                        "id": q["id"],
                        "rerank_max": top_score,
                        "vector_max": maxv,
                        "hit1": r_["hybrid+rerank"] == 1,
                    }
                )
                if r_["hybrid+rerank"] != 1:
                    fails.append(
                        {
                            "id": q["id"],
                            "q": q["question"],
                            "ranks": r_,
                            "top1": reranked[0][0]
                            + " | "
                            + reranked[0][1][:120].replace("\n", " / ")
                            if reranked
                            else None,
                            "top1_score": round(top_score, 3),
                        }
                    )
            if key not in rerank_set:
                ranks.pop("hybrid+rerank")
            report["variants"][label] = {m: metrics(v) for m, v in ranks.items()}
            if key in rerank_set:
                report["unanswerable"][label] = {"unanswerable": unans, "answerable": top1_ans}
            report["failures"][label] = fails
            print(label, json.dumps(report["variants"][label], ensure_ascii=False))
    rr.save()
    await engine.dispose()
    report["cost"] = {
        "query_embed_tokens_this_run": embed_tokens,
        "rerank_calls_this_run": rr.calls,
        "rerank_tokens_this_run": rr.tokens,
        "rerank_yuan_this_run": rr.tokens * RERANK_PRICE / 1e6,
    }
    # 阈值建议
    for label, d in report["unanswerable"].items():
        u = sorted(x["rerank_max"] for x in d["unanswerable"])
        a = sorted(x["rerank_max"] for x in d["answerable"] if x["hit1"])
        best = None
        for t in [i / 100 for i in range(5, 96)]:
            acc = sum(x > t for x in a) + sum(x <= t for x in u)
            if best is None or acc > best[1]:
                best = (t, acc)
        d["summary"] = {
            "unans_rerank_max": [round(x, 3) for x in u],
            "ans_hit1_rerank_top1": {
                "min": round(min(a), 3) if a else None,
                "p10": round(a[len(a) // 10], 3) if a else None,
                "median": round(statistics.median(a), 3) if a else None,
            },
            "best_threshold": best[0],
            "unans_vector_max": [round(x["vector_max"], 3) for x in d["unanswerable"]],
        }
        print(label, json.dumps(d["summary"], ensure_ascii=False))
    (RES / args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str))
    print(json.dumps(report["cost"]))


if __name__ == "__main__":
    asyncio.run(main())
