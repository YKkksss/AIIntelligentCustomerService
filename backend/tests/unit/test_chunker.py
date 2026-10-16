import random

import pytest

from aics.core.tokens import count_tokens
from aics.ingest.chunker import chunk_document
from aics.ingest.parsers.base import Block, ParsedDoc

SENTS = [
    "签收后七天内商品完好可申请无理由退货。",
    "质量问题退货运费由商家承担。",
    "非质量问题退货的退回运费由买家承担！",
    "Refunds are issued to the original payment method within five business days.",
    "换货商品发出后原商品需在七天内寄回；",
    "定制类与生鲜类商品不支持无理由退货？",
]


def _doc(n_paras: int, seed: int = 0) -> ParsedDoc:
    rnd = random.Random(seed)  # noqa: S311  测试数据，固定种子
    blocks = [Block("heading", "售后政策", level=1), Block("heading", "退货", level=2)]
    for _ in range(n_paras):
        blocks.append(
            Block("paragraph", "".join(rnd.choice(SENTS) for _ in range(rnd.randint(1, 12))))
        )
    return ParsedDoc(blocks=blocks)


@pytest.mark.parametrize(("size", "overlap"), [(500, 80), (200, 40), (120, 0), (100, 50)])
def test_lengths_and_overlap(size: int, overlap: int) -> None:
    chunks = chunk_document(_doc(60, seed=size), chunk_size=size, chunk_overlap=overlap)
    assert len(chunks) > 3
    for c in chunks:
        assert 0 < c.tokens <= size, (c.tokens, size)
        assert c.tokens == count_tokens(c.text)
        assert c.heading_path == "售后政策 > 退货"
        assert c.overlap_tokens <= overlap
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    for prev, cur in zip(chunks, chunks[1:], strict=False):
        if overlap == 0:
            assert cur.overlap_tokens == 0
            continue
        head = cur.text.split("\n", 1)[0]
        if cur.overlap_tokens == 0:  # 只有下一单元本身几乎占满 chunk_size 时才放弃重叠
            assert cur.tokens > size - overlap - 2
            continue
        assert prev.text.endswith(head), (prev.text[-60:], head)
        assert count_tokens(head) <= overlap


def test_long_paragraph_split_by_sentence_and_hard_split() -> None:
    long_sentence = "无标点长句" * 400  # 单句远超 chunk_size
    doc = ParsedDoc(blocks=[Block("paragraph", "第一句。" + long_sentence + "。最后一句。")])
    chunks = chunk_document(doc, chunk_size=100, chunk_overlap=0)
    assert all(c.tokens <= 100 for c in chunks)
    assert "".join(c.text.replace("\n", "") for c in chunks) == doc.blocks[0].text


def test_no_cross_section_chunks_or_overlap() -> None:
    doc = ParsedDoc(
        blocks=[
            Block("heading", "A", level=1),
            Block("paragraph", "甲段内容。" * 5),
            Block("heading", "B", level=2),
            Block("paragraph", "乙段内容。" * 5),
            Block("heading", "C", level=1),
            Block("paragraph", "丙段内容。"),
        ]
    )
    chunks = chunk_document(doc, chunk_size=500, chunk_overlap=80)
    assert [c.heading_path for c in chunks] == ["A", "A > B", "C"]
    assert all(c.overlap_tokens == 0 for c in chunks)
    assert "甲" not in chunks[1].text


def test_heading_only_sections_produce_no_chunk() -> None:
    doc = ParsedDoc(
        blocks=[
            Block("heading", "空", level=1),
            Block("heading", "有", level=1),
            Block("paragraph", "正文"),
        ]
    )
    assert [c.heading_path for c in chunk_document(doc)] == ["有"]


def _table(rows: int) -> str:
    lines = ["| 商品 | 价格 | 说明 |", "| --- | --- | --- |"]
    lines += [f"| 商品{i} | {i}元 | 这是第{i}行的说明文字 |" for i in range(rows)]
    return "\n".join(lines)


def test_table_kept_whole_when_fits() -> None:
    t = _table(5)
    doc = ParsedDoc(
        blocks=[Block("paragraph", "前文。" * 60), Block("table", t), Block("paragraph", "后文。")]
    )
    chunks = chunk_document(doc, chunk_size=200, chunk_overlap=40)
    holders = [c for c in chunks if "| 商品 |" in c.text]
    assert len(holders) == 1 and t in holders[0].text
    nxt = chunks[chunks.index(holders[0]) + 1] if holders[0] is not chunks[-1] else None
    if nxt is not None:  # 表格不作为重叠来源
        assert "| 商品" not in nxt.text.split("\n", 1)[0]


def test_large_table_split_by_rows_with_header() -> None:
    chunks = chunk_document(
        ParsedDoc(blocks=[Block("table", _table(80))]), chunk_size=150, chunk_overlap=20
    )
    assert len(chunks) > 2
    for c in chunks:
        assert c.tokens <= 150
        assert c.text.startswith("| 商品 | 价格 | 说明 |\n| --- | --- | --- |")
    body = [ln for c in chunks for ln in c.text.split("\n")[2:]]
    assert len(body) == 80  # 每行恰好出现一次，不重叠


def test_pages_tracked() -> None:
    doc = ParsedDoc(
        blocks=[
            Block("paragraph", "第一页内容。", page=1),
            Block("paragraph", "第二页内容。", page=2),
        ]
    )
    c = chunk_document(doc)[0]
    assert (c.page_from, c.page_to) == (1, 2)


def test_invalid_params() -> None:
    with pytest.raises(ValueError):
        chunk_document(ParsedDoc(), chunk_size=100, chunk_overlap=60)


def test_shrunk_overlap_keeps_whole_sentences() -> None:
    """下一段几乎占满块时缩减重叠：只保留完整句子，不能以半句话开头（E2E 发现）。"""
    first = "说明：首重 1 公斤，续重每公斤计费；时效自出库起计算。"
    big = "北京：首重 11 元，续重每公斤 4 元，时效 2 天。" * 40
    doc = ParsedDoc(blocks=[Block("paragraph", first), Block("paragraph", big)])
    size = count_tokens(big) + count_tokens("时效自出库起计算。") + 3
    chunks = chunk_document(doc, chunk_size=size, chunk_overlap=80)
    assert len(chunks) >= 2
    head = chunks[1].text.split("\n")[0]
    assert head in {
        "时效自出库起计算。",
        "北京：首重 11 元，续重每公斤 4 元，时效 2 天。" * 40,
    } or (chunks[1].overlap_tokens == 0)
    assert not chunks[1].text.startswith("公斤")
