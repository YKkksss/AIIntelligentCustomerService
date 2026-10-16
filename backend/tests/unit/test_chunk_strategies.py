import pytest

from aics.core.tokens import count_tokens
from aics.ingest.chunker import ChunkOptions, split_document
from aics.ingest.parsers.base import Block, ParsedDoc
from aics.ingest.parsers.markdown import parse_markdown_text

LAW = """# 示例管理条例

第一章 总则

第一条 为了规范示例管理，制定本条例。

第二条 本条例适用于示例商城的自营业务。第三条 平台应当公开售后规则。

第二章 退货

第一节 一般规定

第四条 消费者有权在签收后七天内退货。

第五条 退货商品应当完好。
"""


def test_legal_article_per_chunk_with_hierarchy() -> None:
    doc = parse_markdown_text(LAW)
    r = split_document(doc, ChunkOptions(strategy="article", chunk_size=400, chunk_overlap=50))
    texts = [c.text for c in r.chunks]
    assert [t[:3] for t in texts] == ["第一条", "第二条", "第三条", "第四条", "第五条"]
    paths = [c.heading_path for c in r.chunks]
    assert paths[0] == "示例管理条例 > 第一章 总则"
    assert paths[3] == "示例管理条例 > 第二章 退货 > 第一节 一般规定"
    assert all(c.overlap_tokens == 0 for c in r.chunks)


def test_legal_long_article_split_with_overlap() -> None:
    long = "第一条 " + "经营者应当依法履行售后义务，不得拒绝合理的退货要求。" * 40
    r = split_document(
        ParsedDoc(blocks=[Block("paragraph", long)]),
        ChunkOptions(strategy="article", chunk_size=120, chunk_overlap=30),
    )
    assert len(r.chunks) > 2 and all(c.tokens <= 120 for c in r.chunks)
    assert any(c.overlap_tokens > 0 for c in r.chunks[1:])


CONTRACT = """# 采购合同

一、合同标的

1. 甲方向乙方采购办公设备一批，具体清单见附件。
1.1 设备型号以附件为准。
1.2 交付地点为甲方指定仓库。
2. 乙方应在合同签订后十个工作日内交货。
"""


def test_contract_clauses() -> None:
    r = split_document(
        parse_markdown_text(CONTRACT),
        ChunkOptions(strategy="clause", chunk_size=500, chunk_overlap=60),
    )
    assert [c.heading_path for c in r.chunks] == ["采购合同 > 一、合同标的"] * 2
    assert r.chunks[0].text.startswith("1. 甲方") and "1.2 交付地点" in r.chunks[0].text
    assert r.chunks[1].text.startswith("2. 乙方")


FAQ = """# 售后常见问题

## 多久可以退货？

签收后七天内可以申请无理由退货。

问：运费谁承担？
质量问题由商家承担，其他情况由买家承担。

Q: 退款多久到账？

审核通过后一般 1–5 个工作日到账。
"""


def test_faq_one_pair_per_chunk() -> None:
    r = split_document(
        parse_markdown_text(FAQ), ChunkOptions(strategy="faq", chunk_size=300, chunk_overlap=0)
    )
    assert [c.text.split("\n")[0] for c in r.chunks] == [
        "多久可以退货？",
        "问：运费谁承担？",
        "Q: 退款多久到账？",
    ]
    assert all(c.heading_path == "售后常见问题" for c in r.chunks)
    assert "七天" in r.chunks[0].text and "买家" in r.chunks[1].text


def test_faq_long_answer_repeats_question() -> None:
    doc = ParsedDoc(
        blocks=[
            Block("paragraph", "问：如何开发票？"),
            Block("paragraph", "请在订单页面申请开票。" * 60),
        ]
    )
    r = split_document(doc, ChunkOptions(strategy="faq", chunk_size=100, chunk_overlap=0))
    assert len(r.chunks) > 1
    assert all(c.text.startswith("问：如何开发票？\n") and c.tokens <= 100 for c in r.chunks)


PAPER = """# 一种示例检索方法

摘要：本文提出一种示例检索方法，在公开数据集上取得了较好效果。

## 1 引言

检索增强生成是近年的研究热点。

## 参考文献

[1] Zhang S. Retrieval methods. 2024.
[2] Li W. Dense passage retrieval. 2023.
"""


def test_paper_abstract_and_references_separate() -> None:
    r = split_document(
        parse_markdown_text(PAPER),
        ChunkOptions(strategy="section", chunk_size=800, chunk_overlap=120),
    )
    paths = [c.heading_path for c in r.chunks]
    assert paths == [
        "一种示例检索方法 > 摘要",
        "一种示例检索方法 > 1 引言",
        "一种示例检索方法 > 参考文献",
    ]
    assert r.chunks[0].text.startswith("本文提出")


def test_paper_references_no_overlap() -> None:
    refs = "\n".join(
        f"[{i}] Author {i}. A very long reference title number {i}. Journal, 2024."
        for i in range(60)
    )
    doc = ParsedDoc(blocks=[Block("heading", "References", level=2), Block("paragraph", refs)])
    r = split_document(doc, ChunkOptions(strategy="section", chunk_size=200, chunk_overlap=60))
    assert len(r.chunks) > 2 and all(c.overlap_tokens == 0 for c in r.chunks)


def test_custom_separators_literal() -> None:
    doc = ParsedDoc(blocks=[Block("paragraph", "第一部分内容###第二部分内容###(.*)不是正则")])
    r = split_document(doc, ChunkOptions(chunk_size=100, chunk_overlap=0, separators=("###",)))
    assert r.chunks[0].text == "第一部分内容\n第二部分内容\n(.*)不是正则"
    r = split_document(doc, ChunkOptions(chunk_size=100, chunk_overlap=0, separators=("(.*)",)))
    assert "(.*)" not in r.chunks[0].text


def test_separators_create_split_points() -> None:
    a, b = "甲" * 60, "乙" * 60
    doc = ParsedDoc(blocks=[Block("paragraph", f"{a}###{b}")])
    r = split_document(doc, ChunkOptions(chunk_size=200, chunk_overlap=0, separators=("###",)))
    assert [c.text[0] for c in r.chunks] == ["甲", "乙"]


def test_split_by_heading_off_merges_sections() -> None:
    doc = parse_markdown_text("# A\n\n短段一。\n\n## B\n\n短段二。\n")
    on = split_document(doc, ChunkOptions(chunk_size=200, chunk_overlap=0))
    off = split_document(doc, ChunkOptions(chunk_size=200, chunk_overlap=0, split_by_heading=False))
    assert len(on.chunks) == 2 and len(off.chunks) == 1
    assert off.chunks[0].text == "A\n短段一。\nB\n短段二。" and off.chunks[0].heading_path == "A"


def test_keep_tables_off_splits_rows_without_header() -> None:
    table = "| a | b |\n| --- | --- |\n" + "\n".join(
        f"| 行{i} | 说明文字说明文字 |" for i in range(40)
    )
    doc = ParsedDoc(blocks=[Block("table", table)])
    off = split_document(doc, ChunkOptions(chunk_size=100, chunk_overlap=0, keep_tables=False))
    assert sum(c.text.count("| a | b |") for c in off.chunks) == 1


@pytest.mark.parametrize(("parent", "child", "ov"), [(1024, 200, 20), (400, 100, 0), (300, 60, 30)])
def test_parent_child(parent: int, child: int, ov: int) -> None:
    paras = [Block("heading", "退货", level=1)] + [
        Block(
            "paragraph",
            f"第{i}段：签收后七天内商品完好可申请无理由退货，质量问题运费由商家承担。" * 3,
        )
        for i in range(30)
    ]
    paras.append(Block("table", "| 类别 | 时限 |\n| --- | --- |\n| 数码 | 7 天 |"))
    r = split_document(
        ParsedDoc(blocks=paras),
        ChunkOptions(mode="parent_child", parent_size=parent, child_size=child, child_overlap=ov),
    )
    assert r.parents and r.chunks
    assert all(p.tokens <= parent for p in r.parents)
    assert all(c.tokens <= child for c in r.chunks)
    assert {c.parent_ordinal for c in r.chunks} == {p.ordinal for p in r.parents}
    for c in r.chunks:
        p = r.parents[c.parent_ordinal]  # type: ignore[index]
        assert c.heading_path == p.heading_path == "退货"
        body = c.text.split("\n")
        for line in body[1:] if c.overlap_tokens else body:
            assert line in p.text  # 子块内容都来自父块（重叠部分也来自同一父块）
    assert [c.ordinal for c in r.chunks] == list(range(len(r.chunks)))
    assert sum(count_tokens(p.text) for p in r.parents) >= sum(1 for _ in r.parents)


def test_parent_child_no_cross_parent_overlap() -> None:
    doc = ParsedDoc(
        blocks=[
            Block("heading", "A", level=1),
            Block("paragraph", "甲。" * 200),
            Block("heading", "B", level=1),
            Block("paragraph", "乙。" * 200),
        ]
    )
    r = split_document(
        doc, ChunkOptions(mode="parent_child", parent_size=300, child_size=80, child_overlap=20)
    )
    for c in r.chunks:
        assert "甲" not in c.text or "乙" not in c.text
    firsts = {}
    for c in r.chunks:
        firsts.setdefault(c.parent_ordinal, c)
    assert all(c.overlap_tokens == 0 for c in firsts.values())


def test_options_from_mapping_and_validate() -> None:
    o = ChunkOptions.from_mapping(
        {
            "preset": "legal",
            "strategy": "article",
            "separators": ["#"],
            "chunk_size": 400,
            "chunk_overlap": 50,
        }
    )
    assert o.strategy == "article" and o.separators == ("#",)
    with pytest.raises(ValueError):
        split_document(
            ParsedDoc(), ChunkOptions(mode="parent_child", parent_size=300, child_size=300)
        )
    with pytest.raises(ValueError):
        split_document(
            ParsedDoc(), ChunkOptions(mode="parent_child", child_size=100, child_overlap=60)
        )
