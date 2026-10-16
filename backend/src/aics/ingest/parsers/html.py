"""HTML：trafilatura 提取正文为 Markdown，再按 Markdown 解析（文档处理流水线 §3.2）。"""

from __future__ import annotations

import trafilatura
from lxml import html as lxml_html

from aics.ingest.parsers.base import ParsedDoc, ParseError
from aics.ingest.parsers.markdown import parse_markdown_text
from aics.ingest.parsers.text import decode_text


def _title(markup: str) -> str | None:
    try:
        t = lxml_html.fromstring(markup).findtext(".//title")
    except (ValueError, lxml_html.etree.ParserError):  # type: ignore[attr-defined]
        return None
    return t.strip() if t and t.strip() else None


def parse_html_text(markup: str, url: str | None = None) -> ParsedDoc:
    md = trafilatura.extract(
        markup,
        url=url,
        output_format="markdown",
        include_tables=True,
        include_links=False,
        include_images=False,
        include_comments=False,
        favor_recall=True,
        deduplicate=False,
    )
    if not md or not md.strip():
        raise ParseError(message="未能从网页中提取到正文")
    doc = parse_markdown_text(md)
    doc.title = _title(markup)
    return doc


def parse_html(data: bytes) -> ParsedDoc:
    return parse_html_text(decode_text(data))
