"""Markdown：`#` 标题、表格、列表、代码块（文档处理流水线 §3.2）。HTML 提取结果也走这里。"""

from __future__ import annotations

import re

from aics.ingest.parsers.base import Block, ParsedDoc, clean_text
from aics.ingest.parsers.text import decode_text

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_SETEXT = re.compile(r"^(=+|-+)\s*$")
_LIST = re.compile(r"^\s*([-*+]|\d+[.)])\s+")
_TABLE = re.compile(r"^\s*\|.*\|\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_FRONT_MATTER = re.compile(r"\A---\n.*?\n---\n", re.S)
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")


_SEP_ROW = re.compile(r"^\|(\s*:?-{3,}:?\s*\|)+$")


def _normalize_table(lines: list[str]) -> str:
    """统一为带分隔行的 Markdown 表（trafilatura 输出的表可能缺分隔行）。"""
    rows = [ln.strip() for ln in lines if ln.strip()]
    if not rows:
        return ""
    if len(rows) >= 2 and _SEP_ROW.match(rows[1].replace(" ", "")):
        return "\n".join(rows)
    cols = max(1, rows[0].strip("|").count("|") + 1)
    return "\n".join([rows[0], "|" + " --- |" * cols, *rows[1:]])


def _inline(text: str) -> str:
    """去掉图片、保留链接文字；强调符号保留（对检索无害，避免误删正文中的 *）。"""
    return _LINK.sub(r"\1", _IMAGE.sub(r"\1", text))


def parse_markdown_text(text: str) -> ParsedDoc:
    text = _FRONT_MATTER.sub("", text.replace("\r\n", "\n"))
    lines = text.split("\n")
    blocks: list[Block] = []
    buf: list[str] = []
    kind = "paragraph"

    def flush() -> None:
        nonlocal buf, kind
        body = clean_text(_inline("\n".join(buf))) if kind != "table" else _normalize_table(buf)
        if body:
            blocks.append(Block(kind, body))  # type: ignore[arg-type]
        buf, kind = [], "paragraph"

    i = 0
    while i < len(lines):
        line = lines[i]
        if _FENCE.match(line):  # 代码块整体作为一个段落，内部的 # 不当标题
            flush()
            fence = _FENCE.match(line).group(1)  # type: ignore[union-attr]
            i += 1
            code: list[str] = []
            while i < len(lines) and not lines[i].strip().startswith(fence):
                code.append(lines[i])
                i += 1
            if "\n".join(code).strip():
                blocks.append(Block("paragraph", "\n".join(code).strip()))
            i += 1
            continue
        m = _HEADING.match(line)
        if m:
            flush()
            blocks.append(Block("heading", clean_text(_inline(m.group(2))), level=len(m.group(1))))
        elif buf and kind == "paragraph" and len(buf) == 1 and _SETEXT.match(line) and line.strip():
            title = buf[0]
            buf = []
            blocks.append(
                Block("heading", clean_text(_inline(title)), level=1 if "=" in line else 2)
            )
        elif not line.strip():
            flush()
        elif _TABLE.match(line):
            if kind != "table":
                flush()
                kind = "table"
            buf.append(line.strip())
        elif _LIST.match(line) or (kind == "list" and line.startswith((" ", "\t"))):
            if kind != "list":
                flush()
                kind = "list"
            buf.append(line.rstrip())
        else:
            if kind != "paragraph":
                flush()
            buf.append(line)
        i += 1
    flush()
    return ParsedDoc(blocks=blocks)


def parse_markdown(data: bytes) -> ParsedDoc:
    return parse_markdown_text(decode_text(data))
