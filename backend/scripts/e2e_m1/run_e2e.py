"""M1 端到端测试：真实 Celery worker（RabbitMQ）+ 真实百炼向量化 + 本地 PG / Qdrant。

前置：worker 已用同一环境文件启动（见 README.md）。用法：
  set -a; . /tmp/e2e/env; set +a
  uv run python scripts/e2e_m1/run_e2e.py
输出：scripts/e2e_m1/results/e2e_results.json（场景、期望、实际、是否通过）与 apps.json（评测用应用 ID）。
不打印任何密钥。
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select, text

from aics.core.config import get_settings
from aics.core.textsearch import build_tsquery
from aics.db.engine import get_sessionmaker
from aics.db.models import App, Chunk, ChunkParent, Document, Job, User
from aics.modules.apps.service import AppService
from aics.modules.knowledge.service import (
    KnowledgeService,
)
from aics.storage import get_storage
from aics.tasks.celery_app import celery_app
from aics.vectorstore import build_vector_store

HERE = Path(__file__).parent
OUT = HERE / "out"
RES = HERE / "results"
TERMINAL = {"done", "failed"}
MAIN_DOCS = [
    "01-售后服务政策.md",
    "02-平台交易规则.docx",
    "03-多路召回检索方法研究.pdf",
    "04-常见问题.html",
    "05-AirPure空气净化器手册.docx",
    "06-仓储物流服务合同.pdf",
    "07-区域运费与时效表.txt",
    "E01-星河豆规则历次修订.md",
    "E04-SKU价格总表.md",
    "E05-跨境退货政策.html",
    "E06-供应商协作须知.docx",
]
APPS: dict[str, dict[str, Any]] = {
    "main": {"chunk_config": {"preset": "general"}},
    "noprefix": {"chunk_config": {"preset": "general", "contextual_prefix": False}},
    "pc": {
        "chunk_config": {
            "preset": "general",
            "mode": "parent_child",
            "parent_size": 1024,
            "child_size": 200,
        }
    },
    "legal": {"chunk_config": {"preset": "legal"}, "docs": ["02-平台交易规则.docx"]},
    "paper": {"chunk_config": {"preset": "paper"}, "docs": ["03-多路召回检索方法研究.pdf"]},
    "faq": {"chunk_config": {"preset": "faq"}, "docs": ["04-常见问题.html"]},
    "manual": {"chunk_config": {"preset": "manual"}, "docs": ["05-AirPure空气净化器手册.docx"]},
    "contract": {"chunk_config": {"preset": "contract"}, "docs": ["06-仓储物流服务合同.pdf"]},
    "fulldoc": {
        "chunk_config": {
            "preset": "general",
            "mode": "parent_child",
            "parent_mode": "full_doc",
            "full_doc_max_tokens": 4000,
            "child_size": 200,
        },
        "docs": ["01-售后服务政策.md"],
    },
    "ops": {"chunk_config": {"preset": "general"}, "docs": []},
}

results: list[dict[str, Any]] = []
sessions = get_sessionmaker()
storage = get_storage()
store = build_vector_store(get_settings())


def check(sid: str, scenario: str, expected: str, actual: Any, ok: bool) -> None:
    results.append(
        {"id": sid, "scenario": scenario, "expected": expected, "actual": actual, "pass": bool(ok)}
    )
    print(f"[{'PASS' if ok else 'FAIL'}] {sid} {scenario}: {actual}", flush=True)


def enqueue(task: str, args: list[Any]) -> None:
    celery_app.send_task(task, args=list(args))


def ks(s: Any) -> KnowledgeService:
    return KnowledgeService(s, storage=storage, store=store, enqueue=enqueue)


async def call(fn: str, *a: Any, **kw: Any) -> Any:
    async with sessions() as s:
        return await getattr(ks(s), fn)(*a, **kw)


async def doc(doc_id: uuid.UUID) -> Document | None:
    async with sessions() as s:
        return await s.get(Document, doc_id)


async def wait_docs(
    ids: list[uuid.UUID], timeout: float = 900, *, until: set[str] = TERMINAL
) -> dict[uuid.UUID, Document | None]:
    t0 = time.monotonic()
    while True:
        out = {i: await doc(i) for i in ids}
        if all(
            d is None or (d.status in until and d.pending_index_version is None)
            for d in out.values()
        ):
            return out
        if time.monotonic() - t0 > timeout:
            raise TimeoutError({str(k): (v.status if v else None) for k, v in out.items()})
        await asyncio.sleep(1)


async def wait_job(job_id: uuid.UUID, timeout: float = 300) -> Job:
    t0 = time.monotonic()
    while True:
        async with sessions() as s:
            j = await s.get(Job, job_id)
        if j is not None and j.status in ("done", "failed", "cancelled"):
            return j
        if time.monotonic() - t0 > timeout:
            raise TimeoutError(str(job_id))
        await asyncio.sleep(1)


async def counts(app_id: uuid.UUID, doc_id: uuid.UUID | None = None) -> dict[str, int]:
    async with sessions() as s:
        q = (
            select(func.count())
            .select_from(Chunk)
            .join(Document, Document.id == Chunk.document_id)
            .where(Chunk.app_id == app_id, Chunk.index_version == Document.index_version)
        )
        if doc_id:
            q = q.where(Chunk.document_id == doc_id)
        live = (await s.execute(q)).scalar_one()
        flagged = (await s.execute(q.where(Chunk.review_status == "flagged"))).scalar_one()
        retrievable = await ks(s).retrievable_chunk_count(app_id) if doc_id is None else None
    by_doc = await store.count_by_document(app_id, enabled_only=False)
    by_doc_en = await store.count_by_document(app_id, enabled_only=True)
    pts = by_doc.get(doc_id, 0) if doc_id else sum(by_doc.values())
    en = by_doc_en.get(doc_id, 0) if doc_id else sum(by_doc_en.values())
    return {
        "live_chunks": live,
        "points": pts,
        "enabled_points": en,
        "flagged": flagged,
        **({"retrievable": retrievable} if retrievable is not None else {}),
    }


async def reset() -> None:
    """删除上一次运行留下的 e2e-* 应用（PG 行、Qdrant 点、文件）。"""
    async with sessions() as s:
        old = list((await s.execute(select(App).where(App.name.like("e2e-%")))).scalars())
        for a in old:
            await store.delete_app(a.id)
            await storage.delete_prefix(f"{a.id}/")
            for m in (Chunk, ChunkParent, Job, Document):
                await s.execute(delete(m).where(m.app_id == a.id))
            await s.delete(a)
        await s.execute(delete(User).where(User.email == "e2e@xinghe.example"))
        await s.commit()


async def setup() -> dict[str, uuid.UUID]:
    ids = {}
    async with sessions() as s:
        u = User(email="e2e@xinghe.example", display_name="e2e", password_hash="x")
        s.add(u)
        await s.flush()
        for name, spec in APPS.items():
            app = await AppService(s).create_app(
                name=f"e2e-{name}", created_by=u.id, chunk_config=spec["chunk_config"]
            )
            ids[name] = app.id
        await s.commit()
    return ids


async def upload(app_id: uuid.UUID, name: str, filename: str | None = None) -> Any:
    return await call("upload_file", app_id, (OUT / name).read_bytes(), filename or name)


async def expect_error(sid: str, scenario: str, coro: Any, code: str, msg_part: str = "") -> None:
    try:
        await coro
        check(sid, scenario, f"{code}", "无异常", False)
    except Exception as e:  # noqa: BLE001
        c = getattr(e, "code", type(e).__name__)
        m = getattr(e, "message", str(e))
        check(sid, scenario, f"{code} {msg_part}", f"{c}: {m}", c == code and msg_part in m)


async def main() -> None:
    RES.mkdir(exist_ok=True)
    await reset()
    apps = await setup()
    (RES / "apps.json").write_text(json.dumps({k: str(v) for k, v in apps.items()}, indent=1))
    t_start = time.monotonic()

    # ---------------------------------------------------------- 1. 主语料（3 个评测应用 + 预设应用）
    uploaded: dict[str, dict[str, uuid.UUID]] = {}
    for app_name in ("main", "noprefix", "pc"):
        uploaded[app_name] = {}
        for n in MAIN_DOCS:
            uploaded[app_name][n] = (await upload(apps[app_name], n)).document.id
    for app_name, spec in APPS.items():
        for n in spec.get("docs", []):
            uploaded.setdefault(app_name, {})[n] = (await upload(apps[app_name], n)).document.id
    all_ids = [i for m in uploaded.values() for i in m.values()]
    t0 = time.monotonic()
    final = await wait_docs(all_ids, timeout=1800)
    build_s = time.monotonic() - t0
    bad = {str(k): (v.status, v.error) for k, v in final.items() if v is None or v.status != "done"}
    check(
        "S01",
        f"主语料 {len(all_ids)} 份（md/docx/pdf/html/txt-gbk）经真实 worker + 百炼向量化",
        "全部 done",
        bad or f"全部 done，用时 {build_s:.0f}s",
        not bad,
    )
    per_doc = {}
    for app_name, docs in uploaded.items():
        for n, i in docs.items():
            c = await counts(apps[app_name], i)
            per_doc[f"{app_name}/{n}"] = {
                **c,
                "chunk_count": final[i].chunk_count if final[i] else None,
            }
    mism = {
        k: v
        for k, v in per_doc.items()
        if v["points"] != v["live_chunks"] or v["chunk_count"] != v["live_chunks"]
    }
    check(
        "S02",
        "每份文档 Qdrant 点数 = 生效切块数 = chunk_count",
        "一致",
        mism or "全部一致",
        not mism,
    )
    (RES / "per_doc.json").write_text(json.dumps(per_doc, ensure_ascii=False, indent=1))
    inj = per_doc["main/E06-供应商协作须知.docx"]
    check(
        "S03",
        "注入短语文档：flagged 且不进入可检索集",
        "flagged ≥ 1，enabled 点 = 切块 − flagged",
        inj,
        inj["flagged"] >= 1 and inj["enabled_points"] == inj["live_chunks"] - inj["flagged"],
    )
    pc = per_doc["pc/01-售后服务政策.md"]
    async with sessions() as s:
        np_ = (
            await s.execute(
                select(func.count())
                .select_from(ChunkParent)
                .where(ChunkParent.app_id == apps["pc"])
            )
        ).scalar_one()
        fd_par = list(
            (
                await s.execute(
                    select(ChunkParent.tokens).where(ChunkParent.app_id == apps["fulldoc"])
                )
            ).scalars()
        )
        orphan_children = (
            await s.execute(
                select(func.count())
                .select_from(Chunk)
                .where(Chunk.app_id == apps["pc"], Chunk.parent_id.is_(None))
            )
        ).scalar_one()
    check(
        "S04",
        "父子模式：写入父块，子块均有 parent_id",
        "父块 > 0，无孤立子块",
        {"parents": np_, "orphan_children": orphan_children, "d1_children": pc["live_chunks"]},
        np_ > 0 and orphan_children == 0,
    )
    check(
        "S05",
        "full_doc 父块（上限 4000 token）",
        "多个父块，均 ≤ 4000",
        {"parents": len(fd_par), "max_tokens": max(fd_par or [0])},
        len(fd_par) > 1 and max(fd_par) <= 4000,
    )
    async with sessions() as s:
        pts, _ = await store.client.scroll(
            store.chunks_collection, scroll_filter=None, limit=1, with_payload=True
        )
        pc_pt, _ = await store.client.scroll(
            store.chunks_collection,
            limit=1,
            with_payload=True,
            scroll_filter=__import__("qdrant_client").http.models.Filter(
                must=[
                    __import__("qdrant_client").http.models.FieldCondition(
                        key="app_id",
                        match=__import__("qdrant_client").http.models.MatchValue(
                            value=str(apps["pc"])
                        ),
                    )
                ]
            ),
        )
    check(
        "S06",
        "父子模式 Qdrant payload 带 parent_id",
        "有 parent_id",
        list(pc_pt[0].payload) if pc_pt else None,
        bool(pc_pt) and "parent_id" in pc_pt[0].payload,
    )
    preset_chunks = {
        a: per_doc[f"{a}/{APPS[a]['docs'][0]}"]["live_chunks"]
        for a in ("legal", "paper", "faq", "manual", "contract", "fulldoc")
    }
    check(
        "S07",
        "各预设切块数（与 general 对比）",
        "均 > 0",
        {
            **preset_chunks,
            **{
                f"general/{APPS[a]['docs'][0]}": per_doc[f"main/{APPS[a]['docs'][0]}"][
                    "live_chunks"
                ]
                for a in ("legal", "paper", "faq", "manual", "contract")
            },
        },
        all(v > 0 for v in preset_chunks.values()),
    )
    async with sessions() as s:
        art = list(
            (
                await s.execute(
                    select(Chunk.text).where(Chunk.app_id == apps["legal"]).order_by(Chunk.ordinal)
                )
            ).scalars()
        )
        faq = list(
            (
                await s.execute(
                    select(Chunk.text).where(Chunk.app_id == apps["faq"]).order_by(Chunk.ordinal)
                )
            ).scalars()
        )
    starts = sum(t.lstrip().startswith("第") and "条" in t[:8] for t in art)
    check(
        "S08",
        "legal 预设按条切分",
        "多数切块以“第X条”开头",
        f"{starts}/{len(art)}",
        starts >= len(art) * 0.7,
    )
    check(
        "S09",
        "faq 预设一问一块",
        "切块数 ≈ 问题数（49 个三级标题）",
        len(faq),
        40 <= len(faq) <= 60,
    )

    # ---------------------------------------------------------- 2. 边界与错误
    main_id = apps["main"]
    edge: dict[str, Any] = {}
    for n in ("E02-备注.md", "E03-只有标题.md", "E15-控制字符.txt"):
        r = await upload(main_id, n)
        edge[n] = r.document.id
    fin = await wait_docs(list(edge.values()))
    for sid, n, exp in (
        ("S10", "E02-备注.md", "done（1 个切块）"),
        ("S11", "E03-只有标题.md", "failed，提示没有正文"),
        ("S12", "E15-控制字符.txt", "done，控制字符被清除（正文“售后说明”）"),
    ):
        d = fin[edge[n]]
        ok = (
            (d.status == "done" and d.chunk_count >= 1)
            if sid == "S10"
            else (d.status == "failed" and bool(d.error))
        )
        extra = {}
        if sid == "S12":
            async with sessions() as s:
                texts = list(
                    (
                        await s.execute(
                            text("select text from chunks where document_id=:d"), {"d": d.id}
                        )
                    ).scalars()
                )
            extra = {"texts": texts}
            ok = d.status == "done" and texts == ["售后说明"]
        check(
            sid,
            f"边界：{n}",
            exp,
            {"status": d.status, "chunks": d.chunk_count, "error": d.error, **extra},
            ok,
        )
    r = await upload(main_id, "E07-售后服务政策-重复.md")
    check(
        "S13",
        "重复文件（同内容同名）",
        "duplicate=true，返回已有文档",
        {"duplicate": r.duplicate, "same": r.document.id == uploaded["main"]["01-售后服务政策.md"]},
        r.duplicate and r.document.id == uploaded["main"]["01-售后服务政策.md"],
    )
    r = await upload(main_id, "E08-常见问题-副本改名.html")
    check(
        "S14",
        "改名后同内容",
        "duplicate=true",
        {"duplicate": r.duplicate},
        r.duplicate and r.document.id == uploaded["main"]["04-常见问题.html"],
    )
    await expect_error(
        "S15",
        "docx 内容改成 .pdf 扩展名",
        upload(main_id, "E09-手册-错扩展名.pdf"),
        "UNSUPPORTED_FILE_TYPE",
        "不符",
    )
    await expect_error(
        "S16", "纯文本冒充 .pdf", upload(main_id, "E10-假PDF.pdf"), "UNSUPPORTED_FILE_TYPE", "不符"
    )
    await expect_error(
        "S17",
        "带 NUL 的二进制 .txt",
        upload(main_id, "E14-乱码文本.txt"),
        "UNSUPPORTED_FILE_TYPE",
        "不符",
    )
    for sid, n, part in (
        ("S18", "E11-加密合同.pdf", "加密"),
        ("S19", "E12-扫描件.pdf", "扫描件"),
        ("S20", "E13-乱码.txt", "编码"),
    ):
        rr = await upload(main_id, n)
        d = (await wait_docs([rr.document.id]))[rr.document.id]
        check(
            sid,
            f"边界：{n}",
            f"failed，错误含“{part}”",
            {"status": d.status, "error": d.error},
            d.status == "failed" and part in (d.error or ""),
        )
    await expect_error(
        "S21", "空文件", call("upload_file", main_id, b"", "empty.md"), "VALIDATION_ERROR"
    )
    await expect_error(
        "S22",
        "不支持的扩展名 .exe",
        call("upload_file", main_id, b"MZ\x90\x00", "a.exe"),
        "UNSUPPORTED_FILE_TYPE",
    )

    # ---------------------------------------------------------- 3. 替换 / 409 / 重建 / 删除
    ops = apps["ops"]
    d7 = (await upload(ops, "07-区域运费与时效表.txt")).document.id
    long_ = (await upload(ops, "E01-星河豆规则历次修订.md")).document.id
    await wait_docs([d7, long_])
    before = await counts(ops, d7)
    await call(
        "replace_file",
        ops,
        d7,
        (OUT / "07b-区域运费与时效表-修订.txt").read_bytes(),
        "07-区域运费与时效表-2027.txt",
    )
    await expect_error(
        "S23", "构建中再次替换", call("replace_file", ops, d7, b"x" * 10, "x.txt"), "DOCUMENT_BUSY"
    )
    await expect_error("S24", "构建中重建", call("rebuild_document", ops, d7), "DOCUMENT_BUSY")
    mid = await counts(ops, d7)
    await wait_docs([d7])
    await asyncio.sleep(3)  # 等 cleanup 任务
    after = await counts(ops, d7)
    dd = await doc(d7)
    async with sessions() as s:
        txt = " ".join(
            (await s.execute(select(Chunk.text).where(Chunk.document_id == d7))).scalars()
        )
    ok = (
        dd.version == 2
        and dd.index_version == 2
        and "2027" in txt
        and "2026" not in txt
        and after["points"] == after["live_chunks"]
    )
    check(
        "S25",
        "替换为修订内容（GBK txt）",
        "v2 生效、旧版本清理、点数 = 切块",
        {
            "before": before,
            "mid_live": mid["live_chunks"],
            "after": after,
            "version": dd.version,
            "storage_key": dd.storage_key.rsplit("/", 1)[-1],
        },
        ok,
    )
    same = await call(
        "replace_file", ops, d7, (OUT / "07b-区域运费与时效表-修订.txt").read_bytes(), "a.txt"
    )
    check("S26", "替换为相同内容", "unchanged=true", same.unchanged, same.unchanged)
    await expect_error(
        "S27",
        "替换为另一文档的内容",
        call("replace_file", ops, d7, (OUT / "E01-星河豆规则历次修订.md").read_bytes(), "b.md"),
        "DUPLICATE_DOCUMENT",
    )

    # 构建中删除（超长文档）
    j = await call("rebuild_document", ops, long_)
    t_wait = time.monotonic()
    while (await doc(long_)).status != "indexing" and time.monotonic() - t_wait < 120:
        await asyncio.sleep(0.2)
    status_at_delete = (await doc(long_)).status
    t_del = time.monotonic()
    dj = await call("delete_document", ops, long_)
    immediate = await counts(ops, long_)
    await expect_error("S28", "删除中再删除", call("delete_document", ops, long_), "DOCUMENT_BUSY")
    rj = await wait_job(j.id)
    djob = await wait_job(dj.id)
    elapsed = time.monotonic() - t_del
    gone = await doc(long_) is None
    left = await counts(ops, long_)
    files_left = await storage.exists(f"{ops}/{long_}/v1.md")
    check(
        "S29",
        "构建中删除：构建任务中止、数据清理",
        "rebuild job cancelled；文档行 / 点 / 文件清空",
        {
            "status_at_delete": status_at_delete,
            "rebuild_job": rj.status,
            "cleanup_job": djob.status,
            "immediate_enabled_points": immediate["enabled_points"],
            "left": left,
            "row_gone": gone,
            "file_left": files_left,
        },
        rj.status == "cancelled"
        and gone
        and left["points"] == 0
        and not files_left
        and immediate["enabled_points"] == 0,
    )
    check(
        "S30", "删除到向量清理完成的时长（真实队列）", "< 300 秒", f"{elapsed:.1f}s", elapsed < 300
    )

    # 重建（单文档）期间旧版本可检索
    j = await call("rebuild_document", main_id, uploaded["main"]["05-AirPure空气净化器手册.docx"])
    async with sessions() as s:
        before_r = await ks(s).retrievable_chunk_count(main_id)
    samples = []
    while True:
        d = await doc(uploaded["main"]["05-AirPure空气净化器手册.docx"])
        async with sessions() as s:
            samples.append(await ks(s).retrievable_chunk_count(main_id))
        if d.status in TERMINAL and d.pending_index_version is None:
            break
        await asyncio.sleep(0.3)
    await asyncio.sleep(3)
    check(
        "S31",
        "重建期间旧版本持续可检索",
        "可检索切块数始终 = 重建前",
        {"before": before_r, "min": min(samples), "max": max(samples), "samples": len(samples)},
        min(samples) == max(samples) == before_r,
    )

    # ---------------------------------------------------------- 4. 自定义词典 + retokenize（真实 worker）
    long2 = (await upload(ops, "E01-星河豆规则历次修订.md")).document.id
    await wait_docs([long2])
    tsq = "SELECT count(*) FROM chunks c WHERE c.app_id = :a AND c.tsv @@ to_tsquery('simple', '星河豆')"
    async with sessions() as s:
        n0 = (await s.execute(text(tsq), {"a": ops})).scalar_one()
        res = await AppService(s).update_app(ops, {"custom_dict": ["星河豆"]})
        await s.commit()
    pts0 = await store.count_by_document(ops, enabled_only=False)
    job = await call("request_retokenize", ops)
    rj = await wait_job(job.id)
    async with sessions() as s:
        n1 = (await s.execute(text(tsq), {"a": ops})).scalar_one()
        app = await s.get(App, ops)
    q = build_tsquery(ops, "星河豆多少可以抵一块钱", app.custom_dict)
    check(
        "S32",
        "custom_dict 新词“星河豆” → retokenize_app",
        "命中数 0 → >0，点不变，job done",
        {
            "before": n0,
            "after": n1,
            "job": rj.status,
            "points_same": pts0 == await store.count_by_document(ops, enabled_only=False),
            "tsquery": q,
            "dict_changed": res.custom_dict_changed,
        },
        n0 == 0 and n1 > 0 and rj.status == "done" and "'星河豆'" in (q or ""),
    )

    # ---------------------------------------------------------- 5. 对账 / CLI / sweep / beat
    env = dict(__import__("os").environ)
    cli = subprocess.run(
        [sys.executable, "-m", "aics.cli", "reconcile", "--inline"],
        capture_output=True,
        text=True,
        env=env,
        timeout=600,
    )
    rec = (
        json.loads(cli.stdout)
        if cli.stdout.strip().startswith("{")
        else {"raw": cli.stdout[-500:], "err": cli.stderr[-500:]}
    )
    e2e_reports = [a for a in rec.get("apps", []) if a["app_id"] in {str(v) for v in apps.values()}]
    check(
        "S33",
        "aics-cli reconcile --inline（全部应用）",
        "退出码 0，无差异",
        {
            "exit": cli.returncode,
            "diff_count": rec.get("diff_count"),
            "e2e_apps": len(e2e_reports),
            "pg": sum(a["pg"] for a in e2e_reports),
            "qdrant": sum(a["qdrant"] for a in e2e_reports),
        },
        cli.returncode == 0 and rec.get("diff_count") == 0,
    )
    cli2 = subprocess.run(
        [sys.executable, "-m", "aics.cli", "reindex", "--app", str(apps["faq"])],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    jobs = json.loads(cli2.stdout.strip().splitlines()[-1])["jobs"] if cli2.returncode == 0 else []
    jstat = [(await wait_job(uuid.UUID(x))).status for x in jobs]
    check(
        "S34",
        "aics-cli reindex --app（经 broker 投递给 worker）",
        "退出码 0，任务 done",
        {"exit": cli2.returncode, "jobs": jstat, "stderr": cli2.stderr[-200:]},
        cli2.returncode == 0 and jstat and all(s_ == "done" for s_ in jstat),
    )

    # 人为制造孤儿点与遗留旧版本 → 手动投递 sweep 任务（真实 worker）
    from aics.vectorstore import ChunkPoint

    ghost = uuid.uuid4()
    await store.upsert_chunks(
        ops, [ChunkPoint(uuid.uuid4(), ghost, 1, [0.01] * get_settings().embed_dim)]
    )
    r = celery_app.send_task("aics.sweep_knowledge")
    sweep = r.get(timeout=300)
    check(
        "S35",
        "sweep_knowledge 经 worker 清理孤儿点",
        "orphans ≥ 1，孤儿点被删",
        {
            "result": sweep,
            "ghost_left": ghost in await store.count_by_document(ops, enabled_only=False),
        },
        sweep.get("orphans", 0) >= 1
        and ghost not in await store.count_by_document(ops, enabled_only=False),
    )
    r = celery_app.send_task("aics.reconcile", args=[str(main_id), None])
    rr = r.get(timeout=300)
    check(
        "S36",
        "reconcile 任务经 worker 执行（main 应用）",
        "diff_count = 0",
        {
            "diff_count": rr["diff_count"],
            "pg": rr["apps"][0]["pg"],
            "qdrant": rr["apps"][0]["qdrant"],
        },
        rr["diff_count"] == 0,
    )

    total = await counts(main_id)
    check(
        "S37",
        "main 应用总体：Qdrant enabled 点数 = PG 可检索切块数",
        "一致",
        total,
        total["enabled_points"] == total["retrievable"],
    )
    summary = {
        "total_s": round(time.monotonic() - t_start),
        "pass": sum(r_["pass"] for r_ in results),
        "fail": sum(not r_["pass"] for r_ in results),
    }
    (RES / "e2e_results.json").write_text(
        json.dumps(
            {"summary": summary, "results": results}, ensure_ascii=False, indent=1, default=str
        )
    )
    print(json.dumps(summary))
    await store.aclose()


if __name__ == "__main__":
    asyncio.run(main())
