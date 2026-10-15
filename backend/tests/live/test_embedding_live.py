"""真实百炼向量化冒烟（发布前手动：uv run pytest -m live）。Key 只从环境变量读取。"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.live


async def test_dashscope_embedding_live(monkeypatch: pytest.MonkeyPatch) -> None:
    key = os.environ.get("AICS_LIVE_EMBED_API_KEY")
    if not key:
        pytest.skip("未设置 AICS_LIVE_EMBED_API_KEY")
    from aics.core.config import get_settings
    from aics.providers import build_embedder

    monkeypatch.setenv("AICS_EMBED_API_KEY", key)
    get_settings.cache_clear()
    emb = build_embedder(get_settings())
    vecs = await emb.embed_documents(["退货运费由谁承担？", "会员积分怎么兑换", "发票如何开具"])
    assert len(vecs) == 3 and all(len(v) == 1024 for v in vecs)
    q = await emb.embed_query("退货的运费谁出")
    sims = [sum(a * b for a, b in zip(q, v, strict=True)) for v in vecs]
    assert sims[0] == max(sims)  # 语义最近的是第一条
