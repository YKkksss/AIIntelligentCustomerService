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


# 解码后替换字符（U+FFFD）占比超过该值视为乱码（编码误判或二进制内容），拒绝入库
MAX_REPLACEMENT_RATIO = 0.05


def _check_garbled(text: str) -> None:
    visible = [ch for ch in text if not ch.isspace()]
    if not visible:
        return
    bad = sum(1 for ch in visible if ch == "\ufffd")
    if bad / len(visible) > MAX_REPLACEMENT_RATIO:
        raise ParseError(
            message=f"文本疑似乱码（无法解码字符占 {bad / len(visible):.0%}），请确认文件编码"
        )


def parse_txt(data: bytes) -> ParsedDoc:
    text = decode_text(data)
    _check_garbled(text)
    blocks = [Block("paragraph", p) for p in (clean_text(x) for x in _BLANK.split(text)) if p]
    return ParsedDoc(blocks=blocks)
