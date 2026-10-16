"""PDF 行合并与双栏（M1.7）：逐行文本块合并成段、标题 / 列表 / 表格保持、双栏按栏顺序。"""

from __future__ import annotations

import io

import pymupdf

from aics.ingest.parsers.pdf import parse_pdf

CJK = "china-s"


def _wrap_cjk(text: str, per: int) -> list[str]:
    return [text[i : i + per] for i in range(0, len(text), per)]


class _Writer:
    """每行单独 insert_text（每行一个文本块，常见于导出工具生成的 PDF）。"""

    def __init__(self, x0: float = 60, width: float = 470, size: float = 10.5) -> None:
        self.doc = pymupdf.open()
        self.page = self.doc.new_page()
        self.x0, self.width, self.size, self.y = x0, width, size, 70.0

    def para(
        self,
        text: str,
        *,
        indent: int = 2,
        gap: float = 6,
        x0: float | None = None,
        width: float | None = None,
        font: str = CJK,
    ) -> None:
        x = self.x0 if x0 is None else x0
        w = self.width if width is None else width
        per = int(w // self.size)
        first = "　" * indent + text
        for i, ln in enumerate(_wrap_cjk(first, per)):
            # 全角空格不会被渲染成可见字形：用起点偏移表示首行缩进
            if i == 0 and indent:
                self.page.insert_text(
                    (x + indent * self.size, self.y),
                    ln.lstrip("　"),
                    fontname=font,
                    fontsize=self.size,
                )
            else:
                self.page.insert_text((x, self.y), ln, fontname=font, fontsize=self.size)
            self.y += self.size * 1.5
        self.y += gap

    def heading(self, text: str, size: float = 16) -> None:
        self.y += 6
        self.page.insert_text((self.x0, self.y), text, fontname=CJK, fontsize=size)
        self.y += size * 1.8

    def bytes(self) -> bytes:
        return self.doc.tobytes()


P1 = (
    "签收后七天内商品完好可申请无理由退货，退货运费由买家承担；质量问题的往返运费由商家承担，平台在核实后三个工作日内完成退款。"
    * 2
)
P2 = (
    "生鲜类商品不支持无理由退货，如出现腐烂变质，请在签收后四十八小时内拍照申请售后，客服将在一个工作日内处理并原路退款。"
    * 2
)


def test_cjk_lines_merged_into_paragraphs_lists_and_headings_kept() -> None:
    w = _Writer()
    w.heading("第一章 退货规则")
    w.para(P1)
    w.para(P2)
    w.heading("第二章 办理流程")
    for item in ("1. 在订单页点击申请售后并选择原因。", "2. 按页面提示寄回商品并填写运单号。"):
        w.para(item, indent=0, gap=0)
    w.para(
        "3. "
        + "商家收到退货并确认商品状态后，平台在一个工作日内退款到原支付账户，超时未处理的由平台自动退款。"
        * 2,
        indent=0,
    )
    blocks = parse_pdf(w.bytes()).blocks
    kinds = [(b.type, b.text[:8]) for b in blocks]
    paras = [b.text for b in blocks if b.type == "paragraph"]
    assert [b.text for b in blocks if b.type == "heading"] == ["第一章 退货规则", "第二章 办理流程"]
    assert paras[0] == P1 and paras[1] == P2, kinds  # 多行合并成一段、中文无空格
    assert paras[2].startswith("1. ") and paras[3].startswith("2. ")  # 列表项不合并
    assert paras[4].startswith("3. ") and paras[4].endswith("自动退款。") and "\n" not in paras[4]
    assert len(paras) == 5


def test_unindented_paragraphs_split_by_short_last_line() -> None:
    w = _Writer()
    w.para(P1, indent=0, gap=0)  # 无缩进、无额外段距：靠段末短行分段
    w.para(P2, indent=0, gap=0)
    paras = [b.text for b in parse_pdf(w.bytes()).blocks]
    assert paras == [P1, P2]


def test_english_hyphenation_and_spaces() -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    lines = [
        "Refunds are issued to the original payment method within five business days after the",
        "returned item has been inspected by our warehouse team and the inspection infor-",
        "mation has been recorded in the order history for future reference.",
        "Short closing line.",
    ]
    # 前三行排满到同一右边界（两端对齐的效果用逐词间距难以复现，这里把前两行补到相同宽度）
    widths = [pymupdf.get_text_length(t, fontsize=10) for t in lines]
    full = max(widths)
    y = 80.0
    for t, wd in zip(lines, widths, strict=True):
        size = 10 * (full / wd) if wd < full and t != lines[-2] and t != lines[-1] else 10
        page.insert_text((60, y), t, fontsize=min(size, 10.4))
        y += 14
    text = [b.text for b in parse_pdf(doc.tobytes()).blocks]
    assert "inspection information has been recorded" in text[0]
    assert "after the returned item" in text[0]


def test_two_column_reading_order() -> None:
    w = _Writer()
    w.heading("双栏版式测试")
    left, right = "左栏内容讲退货规则。" * 12, "右栏内容讲换货规则。" * 12
    top = w.y
    w.para(left, indent=0, x0=50, width=230)
    w.y = top
    w.para(right, indent=0, x0=320, width=230)
    blocks = parse_pdf(w.bytes()).blocks
    paras = [b.text for b in blocks if b.type == "paragraph"]
    assert paras == [left, right]


def test_story_layout_pdf_with_list_and_table() -> None:
    html = """
    <h1>Warehouse Service Agreement</h1>
    <p>The warehouse operator shall store, pick, pack and ship goods on behalf of the merchant
    in accordance with the service levels set out in this agreement, and shall keep accurate
    inventory records that can be audited by the merchant at any reasonable time.</p>
    <ul><li>Orders received before 15:00 ship the same day.</li>
    <li>Orders received after 15:00 ship before 12:00 the next day.</li></ul>
    <table border="1"><tr><th>Item</th><th>Fee</th></tr><tr><td>Storage</td><td>1.8</td></tr>
    <tr><td>Picking</td><td>0.6</td></tr></table>
    <p>Lost goods are compensated at full value, capped at five thousand per shipment.</p>
    """
    story = pymupdf.Story(html=html)
    out = io.BytesIO()
    writer = pymupdf.DocumentWriter(out)
    mediabox = pymupdf.paper_rect("a4")
    more = True
    while more:
        dev = writer.begin_page(mediabox)
        more, _ = story.place(mediabox + (60, 60, -60, -60))
        story.draw(dev)
        writer.end_page()
    writer.close()
    blocks = parse_pdf(out.getvalue()).blocks
    texts = [b.text for b in blocks]
    para = next(t for t in texts if t.startswith("The warehouse operator"))
    assert para.endswith("at any reasonable time.") and "\n" not in para
    # 列表项各自成段（不并入上一段，也不互相合并）
    items = [t for t in texts if "Orders received" in t]
    assert len(items) == 2 and items[0].endswith("same day.")
    assert "Storage 1.8" in texts  # 纯空格 span 保留词间距
    assert any(t.startswith("Lost goods") for t in texts)
    assert blocks[0].type == "heading"
