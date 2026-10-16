"""第二轮评测用应用（M1.7 后）：真实 worker + 百炼向量化。

- e2e-main：general 预设，主语料（PDF 行合并后重新解析）。
- e2e-mainlegal：同上，但 02-平台交易规则.docx 带文档级 chunk_config `{"preset": "legal"}`。
结果写 results/apps_r2.json，供 `eval_retrieval.py --apps apps_r2.json --variants main,mainlegal` 使用。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

from sqlalchemy import func, select

from aics.db.models import Chunk, Document, User
from aics.modules.apps.service import AppService
from scripts.e2e_m1 import run_e2e as e

LEGAL_DOC = "02-平台交易规则.docx"


async def main() -> None:
    await e.reset()
    async with e.sessions() as s:
        u = User(email="e2e@xinghe.example", display_name="e2e", password_hash="x")
        s.add(u)
        await s.flush()
        ids = {}
        for key in ("main", "mainlegal"):
            app = await AppService(s).create_app(
                name=f"e2e-{key}", created_by=u.id, chunk_config={"preset": "general"}
            )
            ids[key] = app.id
        await s.commit()
    t0 = time.monotonic()
    docs = []
    for key, app_id in ids.items():
        for name in e.MAIN_DOCS:
            kw = (
                {"chunk_config": {"preset": "legal"}}
                if key == "mainlegal" and name == LEGAL_DOC
                else {}
            )
            r = await e.call("upload_file", app_id, (e.OUT / name).read_bytes(), name, **kw)
            docs.append(r.document.id)
    fin = await e.wait_docs(docs)
    bad = [str(d.title) for d in fin.values() if d is None or d.status != "done"]
    async with e.sessions() as s:
        rows = (
            await s.execute(
                select(
                    Document.app_id,
                    Document.title,
                    Document.chunk_count,
                    Document.chunk_config.is_not(None),
                )
                .where(Document.app_id.in_(list(ids.values())))
                .order_by(Document.title)
            )
        ).all()
        total = await s.scalar(
            select(func.count()).select_from(Chunk).where(Chunk.app_id.in_(list(ids.values())))
        )
    out = {
        "seconds": round(time.monotonic() - t0, 1),
        "failed": bad,
        "chunks_total": total,
        "per_doc": [
            {
                "app": next(k for k, v in ids.items() if v == r[0]),
                "title": r[1],
                "chunks": r[2],
                "override": r[3],
            }
            for r in rows
        ],
    }
    (e.RES / "apps_r2.json").write_text(json.dumps({k: str(v) for k, v in ids.items()}, indent=1))
    (e.RES / "build_r2.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(json.dumps(out, ensure_ascii=False, indent=1))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    asyncio.run(main())
