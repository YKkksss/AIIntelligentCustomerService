"""PDF：PyMuPDF（文档处理流水线 §3.2）。

- 正文字号 = 按字符数加权的众数字号；字号 ≥ 正文 × 1.15 且行短（≤ 60 字）的行判为标题，
  按字号从大到小映射为 1–3 级。
- 页眉页脚：位于页面上下 8% 区域、去掉数字后在 ≥ 50% 页面（至少 3 页）重复出现的行删除；纯页码行删除。
- 表格：`page.find_tables()` 识别，整表转 Markdown，表格区域内的文字不再作为段落。
- 无文本层（扫描件）→ 解析失败（v1 不做 OCR）。
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


@dataclass
class _Line:
    page: int
    text: str
    size: float
    y0: float
    y1: float
    block_no: int
    in_margin: bool


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
                spans = [s for s in ln.get("spans", []) if s.get("text", "").strip()]
                if not spans:
                    continue
                bbox = pymupdf.Rect(ln["bbox"])
                if any(r.intersects(bbox) for r in rects):
                    continue
                text = clean_text("".join(s["text"] for s in spans))
                if not text:
                    continue
                size = max(s["size"] for s in spans)
                in_margin = bbox.y1 < height * MARGIN_RATIO or bbox.y0 > height * (1 - MARGIN_RATIO)
                lines.append(_Line(pno, text, round(size, 1), bbox.y0, bbox.y1, bno, in_margin))
    return lines, tables


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
                or prev.block_no != ln.block_no
                or (ln.y0 - prev.y1) > ln.size * 0.8
                or (prev.size in level_of)
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
