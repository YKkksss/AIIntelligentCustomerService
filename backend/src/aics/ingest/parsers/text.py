"""TXT：自动检测编码，按空行分段（文档处理流水线 §3.2）。"""

from __future__ import annotations

import re

from charset_normalizer import from_bytes

from aics.ingest.parsers.base import Block, ParsedDoc, ParseError, clean_text

_BLANK = re.compile(r"\n\s*\n")


def decode_text(data: bytes) -> str:
    """BOM → UTF-8 严格解码 → charset-normalizer 检测（GBK 类统一按 GB18030 解码）。"""
    for bom, enc in ((b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16")):
        if data.startswith(bom):
            return data.decode(enc, errors="replace").lstrip("\ufeff")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    best = from_bytes(data).best()
    if best is None:
        raise ParseError(message="无法识别文本编码")
    enc = best.encoding.lower()
    if enc in {"gb2312", "gbk", "gb18030", "hz"}:
        enc = "gb18030"
    return data.decode(enc, errors="replace")


def parse_txt(data: bytes) -> ParsedDoc:
    text = decode_text(data)
    blocks = [Block("paragraph", p) for p in (clean_text(x) for x in _BLANK.split(text)) if p]
    return ParsedDoc(blocks=blocks)
