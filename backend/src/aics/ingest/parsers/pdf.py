"""PDF：PyMuPDF（文档处理流水线 §3.2）。

- 正文字号 = 按字符数加权的众数字号；字号 ≥ 正文 × 1.15 且行短（≤ 60 字）的行判为标题，
  按字号从大到小映射为 1–3 级。
- 页眉页脚：位于页面上下 8% 区域、去掉数字后在 ≥ 50% 页面（至少 3 页）重复出现的行删除；纯页码行删除。
- 表格：`page.find_tables()` 识别，整表转 Markdown，表格区域内的文字不再作为段落。
- 无文本层（扫描件）→ 解析失败（v1 不做 OCR）。
- 行合并成段（M1.7）：不依赖 PyMuPDF 的文本块划分（很多 PDF 每行一个块）。同栏相邻两行满足
  “同字体同字号、行距 ≤ 0.8 × 字号、上一行排满（下一行首词放不进上一行剩余空间）、下一行不是列表项且无首行缩进”
  时视为同一段；上一行未排满（段末短行）即分段。中文直接拼接、英文加空格、连字符断词拼回。
  跨页时上一段未以句末标点结束则续接。标题、表格不参与合并。
- 双栏：某页左右两半各有 ≥ 25% 的正文行且横跨中线的行 < 15% 时按“通栏顶部 → 左栏 → 右栏 → 通栏其余”排序。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

import pymupdf

from aics.ingest.parsers.base import Block, ParsedDoc, ParseError, clean_text, table_to_markdown

HEADING_RATIO = 1.15
HEADING_MAX_CHARS = 60
MARGIN_RATIO = 0.08
_DIGITS = re.compile(r"\d+")
_PAGE_NO = re.compile(
    r"^(第\s*)?[-—\s]*\d+\s*(页)?[-—\s]*(/\s*\d+)?$|^page\s*\d+(\s*of\s*\d+)?$", re.I
)
_CJK_END = re.compile(r"[\u4e00-\u9fff，。；：、！？）」』】]$")
pymupdf.no_recommend_layout()  # 不向 stdout 打印推荐信息

_CJK_START = re.compile(r"^[\u4e00-\u9fff，。；：、！？（「『【]")
# 列表项 / 条款开头：不与上一行合并
_LIST_START = re.compile(
    r"^(?:[-•·●○■□◆▪*]\s|\d{1,3}[.)、．]\s?\S|[（(]\s*[0-9一二三四五六七八九十]{1,3}\s*[）)]|"
    r"[一二三四五六七八九十]{1,3}、|[a-zA-Z][.)]\s|第[一二三四五六七八九十百零〇0-9]+[条章节款])"
)
_FIRST_WORD = re.compile(r"^[A-Za-z0-9'’\-]+")
INDENT_MIN = 1.5  # 首行缩进 ≥ 1.5 个字宽视为新段
COL_MIN_SHARE, COL_SPAN_MAX = 0.25, 0.15


@dataclass
class _Line:
    page: int
    text: str
    size: float
    y0: float
    y1: float
    block_no: int
    in_margin: bool
    x0: float = 0.0
    x1: float = 0.0
    font: str = ""
    col: int = 0  # 0 = 通栏 / 单栏，1 = 左栏，2 = 右栏


def _join(a: str, b: str) -> str:
    """同一段内换行合并：中文之间不加空格，英文之间加空格，连字符断词直接拼接。"""
    if a.endswith("-") and b[:1].isalpha():
        return a[:-1] + b
    if _CJK_END.search(a) or _CJK_START.match(b):
        return a + b
    return a + " " + b


def _collect(doc: pymupdf.Document) -> tuple[list[_Line], dict[int, list[str]]]:
    lines: list[_Line] = []
    tables: dict[int, list[str]] = {}
    for pno, page in enumerate(doc, start=1):
        height = page.rect.height or 1
        rects = []
        try:
            found = page.find_tables()
            for t in found.tables:
                md = table_to_markdown([[c or "" for c in row] for row in t.extract()])
                if md:
                    tables.setdefault(pno, []).append(f"{t.bbox[1]:09.2f}\x00{md}")
                    rects.append(pymupdf.Rect(t.bbox))
        except Exception:  # 表格识别失败不影响正文
            rects = []
        data = page.get_text("dict", sort=True)
        for bno, block in enumerate(data.get("blocks", [])):
            if block.get("type") != 0:
                continue
            for ln in block.get("lines", []):
                all_spans = ln.get("spans", [])
                spans = [s for s in all_spans if s.get("text", "").strip()]
                if not spans:
                    continue
                bbox = pymupdf.Rect(ln["bbox"])
                if any(r.intersects(bbox) for r in rects):
                    continue
                text = clean_text(
                    "".join(s.get("text", "") for s in all_spans)
                )  # 保留纯空格 span（词间距）
                if not text:
                    continue
                size = max(s["size"] for s in spans)
                font = Counter(s.get("font", "") for s in spans).most_common(1)[0][0]
                in_margin = bbox.y1 < height * MARGIN_RATIO or bbox.y0 > height * (1 - MARGIN_RATIO)
                lines.append(
                    _Line(
                        pno,
                        text,
                        round(size, 1),
                        bbox.y0,
                        bbox.y1,
                        bno,
                        in_margin,
                        bbox.x0,
                        bbox.x1,
                        font,
                    )
                )
    return _order_columns(lines, {p: pg.rect.width for p, pg in enumerate(doc, start=1)}), tables


def _order_columns(lines: list[_Line], widths: dict[int, float]) -> list[_Line]:
    """逐页判断是否双栏；双栏页按 通栏顶部 → 左栏 → 右栏 → 通栏其余 排序。"""
    out: list[_Line] = []
    for page in sorted({ln.page for ln in lines}):
        pls = [ln for ln in lines if ln.page == page]
        body = [ln for ln in pls if not ln.in_margin]
        mid = widths.get(page, 0) / 2
        left = [ln for ln in body if ln.x1 <= mid]
        right = [ln for ln in body if ln.x0 >= mid]
        span = [ln for ln in body if ln.x0 < mid < ln.x1]
        n = len(body) or 1
        if not (
            len(left) / n >= COL_MIN_SHARE
            and len(right) / n >= COL_MIN_SHARE
            and len(span) / n < COL_SPAN_MAX
        ):
            out.extend(pls)
            continue
        top = min(ln.y0 for ln in left + right)
        for ln in left:
            ln.col = 1
        for ln in right:
            ln.col = 2
        head = [ln for ln in pls if ln.col == 0 and ln.y0 < top]
        tail = [ln for ln in pls if ln.col == 0 and ln.y0 >= top]
        key = lambda ln: (ln.y0, ln.x0)  # noqa: E731
        out.extend(sorted(head, key=key) + sorted(left, key=key) + sorted(right, key=key))
        out.extend(sorted(tail, key=key))
    return out


def _bounds(lines: list[_Line], body_size: float) -> dict[tuple[int, int], tuple[float, float]]:
    """每页每栏正文行的左右边界（用于判断首行缩进与排满）。"""
    out: dict[tuple[int, int], tuple[float, float]] = {}
    for ln in lines:
        if abs(ln.size - body_size) > 0.6:
            continue
        k = (ln.page, ln.col)
        lo, hi = out.get(k, (ln.x0, ln.x1))
        out[k] = (min(lo, ln.x0), max(hi, ln.x1))
    return out


def _first_word_width(text: str, unit: float) -> float:
    """下一行首词的估计宽度：中文按 1 个字宽；英文单词按 0.5 字宽 / 字符，另加一个空格。"""
    m = _FIRST_WORD.match(text)
    if m:
        return (len(m.group()) + 1) * 0.5 * unit
    return unit


def _continues(prev: _Line, ln: _Line, bounds: dict[tuple[int, int], tuple[float, float]]) -> bool:
    """同页同栏的下一行是否续接上一行所在段落。"""
    if ln.col != prev.col or ln.font != prev.font or abs(ln.size - prev.size) > 0.6:
        return False
    if not (-0.5 * ln.size <= ln.y0 - prev.y1 <= ln.size * 0.8):
        return False
    lo, hi = bounds.get((ln.page, ln.col), (prev.x0, prev.x1))
    unit = ln.size or 1.0
    # 上一行未排满：若下一行第一个词放得进上一行的剩余空间，说明上一行是主动断行（段末）
    if hi - prev.x1 > _first_word_width(ln.text, unit) + 0.5 * unit:
        return False
    if ln.x0 > lo + INDENT_MIN * unit:  # 首行缩进：新段
        return False
    return not _LIST_START.match(ln.text)


def _repeated_margin_lines(lines: list[_Line], pages: int) -> set[str]:
    if pages < 3:
        return set()
    seen: dict[str, set[int]] = {}
    for ln in lines:
        if ln.in_margin:
            seen.setdefault(_DIGITS.sub("#", ln.text), set()).add(ln.page)
    return {k for k, v in seen.items() if len(v) >= max(3, pages / 2)}


def parse_pdf(data: bytes) -> ParsedDoc:
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as e:
        raise ParseError(message="PDF 文件已损坏或无法打开") from e
    with doc:
        if doc.needs_pass:
            raise ParseError(message="PDF 已加密，请先移除密码")
        pages = doc.page_count
        lines, tables = _collect(doc)
        title = clean_text((doc.metadata or {}).get("title") or "") or None

    repeated = _repeated_margin_lines(lines, pages)
    lines = [
        ln
        for ln in lines
        if not (ln.in_margin and (_DIGITS.sub("#", ln.text) in repeated or _PAGE_NO.match(ln.text)))
    ]
    if not lines and not tables:
        raise ParseError(message="PDF 中没有可提取的文本（可能是扫描件，暂不支持 OCR）")

    weights: Counter[float] = Counter()
    for ln in lines:
        weights[ln.size] += len(ln.text)
    body = weights.most_common(1)[0][0] if weights else 0.0
    heading_sizes = sorted(
        {ln.size for ln in lines if ln.size >= body * HEADING_RATIO}, reverse=True
    )[:3]
    level_of = {s: i + 1 for i, s in enumerate(heading_sizes)}
    bounds = _bounds(lines, body)

    blocks: list[Block] = []
    para: list[str] = []
    para_page = 0
    prev: _Line | None = None

    def flush() -> None:
        nonlocal para
        if para:
            text = para[0]
            for nxt in para[1:]:
                text = _join(text, nxt)
            blocks.append(Block("paragraph", text, page=para_page))
            para = []

    pending_tables = {p: sorted(v) for p, v in tables.items()}

    def emit_tables(page: int, before_y: float | None) -> None:
        rest = []
        for item in pending_tables.get(page, []):
            y, md = item.split("\x00", 1)
            if before_y is None or float(y) <= before_y:
                flush()
                blocks.append(Block("table", md, page=page))
            else:
                rest.append(item)
        pending_tables[page] = rest

    for ln in lines:
        if prev is not None and ln.page != prev.page:
            emit_tables(prev.page, None)
        emit_tables(ln.page, ln.y0)
        is_heading = ln.size in level_of and len(ln.text) <= HEADING_MAX_CHARS
        if is_heading:
            flush()
            if (
                blocks
                and blocks[-1].type == "heading"
                and prev is not None
                and (prev.size == ln.size and prev.page == ln.page and prev.block_no == ln.block_no)
            ):  # 标题折行
                last = blocks.pop()
                blocks.append(Block("heading", _join(last.text, ln.text), last.level, last.page))
            else:
                blocks.append(Block("heading", ln.text, level=level_of[ln.size], page=ln.page))
        else:
            new_para = (
                prev is None
                or prev.page != ln.page
                or (prev.size in level_of)
                or not _continues(prev, ln, bounds)
            )
            if new_para and not (
                prev is not None and prev.page != ln.page and para and not _ends_sentence(para[-1])
            ):
                flush()
                para_page = ln.page
            para.append(ln.text)
        prev = ln
    flush()
    for p in sorted(pending_tables):
        emit_tables(p, None)
    return ParsedDoc(blocks=blocks, title=title)


def _ends_sentence(text: str) -> bool:
    return text.endswith(("。", "！", "？", ".", "!", "?", "：", ":", "；", ";"))
