"""M1.4：预处理、full_doc 父块、子块分隔符、向量化文本、预览。"""

from __future__ import annotations

from typing import Any

import pytest

from aics.core.tokens import count_tokens
from aics.ingest.chunker import ChunkOptions, split_document
from aics.ingest.indexer import build_embed_text
from aics.ingest.parsers.base import Block, ParsedDoc
from aics.ingest.preprocess import preprocess
from aics.ingest.service import IngestService, preview_result
from aics.rag.config import ChunkConfig

MD = (
    "# 售后政策\n\n## 退货\n\n"
    + "签收后七天内可申请无理由退货。质量问题运费由商家承担。\n\n" * 30
    + "## 换货\n\n"
    + "换货商品发出后原商品需在七天内寄回。\n\n" * 30
).encode()


def test_preprocess_toggles() -> None:
    doc = ParsedDoc(
        blocks=[
            Block("heading", "联系 https://x.io", level=1),
            Block(
                "paragraph", "邮件  a.b@x.io   或访问 https://x.io/p?q=1 。\n\n\n\n下一段\t\t文字"
            ),
        ]
    )
    same = preprocess(doc, normalize_whitespace=False, remove_urls_emails=False)
    assert same.blocks[1].text == doc.blocks[1].text
    out = preprocess(doc, normalize_whitespace=True, remove_urls_emails=True)
    t = out.blocks[1].text
    assert "@" not in t and "https" not in t and "  " not in t and "\n\n\n" not in t
    assert out.blocks[0].text == "联系 https://x.io"  # 标题不删网址
    ws = preprocess(doc, normalize_whitespace=True, remove_urls_emails=False)
    assert "a.b@x.io" in ws.blocks[1].text


def test_child_separators_apply_only_to_children() -> None:
    para = "；".join(f"第{i}条规则内容较长需要切分" for i in range(40))
    doc = ParsedDoc(blocks=[Block("heading", "规则", level=1), Block("paragraph", para)])
    o = ChunkOptions.from_mapping(
        {
            "mode": "parent_child",
            "parent_size": 800,
            "child_size": 60,
            "chunk_overlap": 0,
            "separators": ["\n\n"],
            "child_separators": ["；"],
        }
    )
    r = split_document(doc, o)
    assert r.parents and all(c.parent_ordinal is not None for c in r.chunks)
    assert all(c.tokens <= 60 for c in r.chunks)
    # 子块按“；”切开（分隔处换行），父块仍按段落分隔符保持原文
    assert all("；" not in c.text for c in r.chunks)
    assert all(line.startswith("第") for c in r.chunks for line in c.text.splitlines())
    assert "；" in r.parents[0].text


def _md_doc() -> ParsedDoc:
    from aics.ingest.parsers import parse_bytes

    return parse_bytes(MD, "md")


def test_full_doc_parents_respect_cap_and_lose_nothing() -> None:
    cfg = ChunkConfig.model_validate(
        {
            "mode": "parent_child",
            "parent_mode": "full_doc",
            "full_doc_max_tokens": 1000,
            "child_size": 120,
            "chunk_overlap": 20,
        }
    ).model_dump()
    r = split_document(_md_doc(), ChunkOptions.from_mapping(cfg))
    assert len(r.parents) >= 2  # 超出上限时开新父块（不截断）
    for p in r.parents:
        assert p.tokens <= 1000 and p.tokens == count_tokens(p.text)
    assert {c.parent_ordinal for c in r.chunks} == {p.ordinal for p in r.parents}
    joined = "".join(p.text for p in r.parents)
    assert joined.count("签收后七天内可申请无理由退货") == 30
    assert joined.count("换货商品发出后") == 30
    assert "换货" in r.parents[-1].text


def test_full_doc_window_uses_cap() -> None:
    c = ChunkConfig.model_validate(
        {
            "mode": "parent_child",
            "parent_mode": "full_doc",
            "full_doc_max_tokens": 3000,
            "child_size": 200,
        }
    )
    assert c.context_chunk_tokens == 3000
    with pytest.raises(ValueError):
        ChunkConfig.model_validate({"mode": "parent_child", "full_doc_max_tokens": 500})


def test_build_embed_text() -> None:
    assert (
        build_embed_text("退货政策", "售后 > 退货", "正文")
        == "文档：退货政策\n章节：售后 > 退货\n正文"
    )
    assert build_embed_text(None, "", "正文") == "正文"
    assert build_embed_text("T", "A", "正文", contextual_prefix=False) == "A\n正文"


class _NoEmbed:
    model, dim = "x", 8

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise AssertionError("预览不应向量化")

    async def embed_query(self, text: str) -> list[float]:
        raise AssertionError


async def test_preview_matches_build_split_and_never_embeds() -> None:
    cfg: dict[str, Any] = ChunkConfig.model_validate({"preset": "manual"}).model_dump()
    svc = IngestService(None, storage=None, store=None, embedder=_NoEmbed(), settings=None)  # type: ignore[arg-type]
    out = await svc.preview(MD, "a.md", cfg, title="售后手册", limit=3)
    expect = split_document(_md_doc(), ChunkOptions.from_mapping(cfg))
    assert out["total_chunks"] == len(expect.chunks) and len(out["chunks"]) == 3
    assert out["total_tokens"] == sum(c.tokens for c in expect.chunks)
    first = out["chunks"][0]
    assert first["text"] == expect.chunks[0].text
    assert first["embed_text"].startswith("文档：售后手册\n章节：")
    assert "文档：" not in first["text"]  # 正文保持原文
    big = preview_result(expect, title="t", chunk_config=cfg, limit=10_000)
    assert len(big["chunks"]) == min(200, len(expect.chunks))
