"""DOCX：python-docx，按样式 Heading 1–6 / 标题 1–6 识别标题，表格转 Markdown（文档处理流水线 §3.2）。"""

from __future__ import annotations

import io
import re
import zipfile

import docx
from docx.document import Document as DocxDocument
from docx.table import Table
from docx.text.paragraph import Paragraph

from aics.ingest.parsers.base import Block, ParsedDoc, ParseError, clean_text, table_to_markdown

MAX_UNZIPPED_BYTES = 200 * 1024 * 1024  # 防 zip 炸弹（安全设计 §4）
_HEADING_STYLE = re.compile(r"^(?:heading|标题)\s*([1-6])$", re.I)


def _check_zip(data: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            total = sum(i.file_size for i in zf.infolist())
    except zipfile.BadZipFile as e:
        raise ParseError(message="DOCX 文件已损坏") from e
    if total > MAX_UNZIPPED_BYTES:
        raise ParseError(message="DOCX 解压后超过 200MB")


def _paragraph_block(p: Paragraph) -> Block | None:
    text = clean_text(p.text)
    if not text:
        return None
    style = (p.style.name if p.style is not None else "") or ""
    m = _HEADING_STYLE.match(style.strip())
    if m:
        return Block("heading", text, level=int(m.group(1)))
    if style.lower() == "title":
        return Block("heading", text, level=1)
    is_list = "list" in style.lower() or p._p.pPr is not None and p._p.pPr.numPr is not None
    return Block("list" if is_list else "paragraph", f"- {text}" if is_list else text)


def _table_rows(t: Table) -> list[list[str]]:
    rows: list[list[str]] = []
    for r in t.rows:
        cells: list[str] = []
        prev = None
        for c in r.cells:  # 合并单元格在 python-docx 中重复返回同一对象，只保留一次
            if c._tc is prev:
                continue
            prev = c._tc
            cells.append(c.text)
        rows.append(cells)
    return rows


def parse_docx(data: bytes) -> ParsedDoc:
    _check_zip(data)
    try:
        d: DocxDocument = docx.Document(io.BytesIO(data))
    except Exception as e:  # python-docx 对损坏文件抛多种异常
        raise ParseError(message="DOCX 文件无法打开") from e
    blocks: list[Block] = []
    list_buf: list[str] = []

    def flush_list() -> None:
        if list_buf:
            blocks.append(Block("list", "\n".join(list_buf)))
            list_buf.clear()

    for item in d.iter_inner_content():
        if isinstance(item, Paragraph):
            b = _paragraph_block(item)
            if b is None:
                continue
            if b.type == "list":
                list_buf.append(b.text)
                continue
            flush_list()
            blocks.append(b)
        else:
            flush_list()
            md = table_to_markdown(_table_rows(item))
            if md:
                blocks.append(Block("table", md))
    flush_list()
    title = clean_text(d.core_properties.title or "") or None
    return ParsedDoc(blocks=blocks, title=title)
