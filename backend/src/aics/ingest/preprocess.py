"""文本预处理（文档处理流水线 §3 第 2.5 步，设计讨论 0001 A2）。

- normalize_whitespace：行内连续空白（含全角空格、制表符）合并为一个空格；3 个以上连续换行合并为一个空行。
- remove_urls_emails：删除 http(s)/www 链接与邮箱地址（表格内同样删除，但保留表格结构）。
只作用于段落、列表、表格的文本；标题不做 URL 删除（标题中出现链接极少，删除反而可能清空标题）。
"""

from __future__ import annotations

import re
from dataclasses import replace

from aics.ingest.parsers.base import Block, ParsedDoc

_INLINE_WS = re.compile(r"[ \t\u00a0\u3000]{2,}|\t")
_MANY_NL = re.compile(r"\n{3,}")
_URL = re.compile(r"(?:https?://|www\.)[^\s<>\"'）)\]】，。；]+", re.I)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def clean(text: str, *, normalize_whitespace: bool, remove_urls_emails: bool, table: bool) -> str:
    if remove_urls_emails:
        text = _EMAIL.sub("", _URL.sub("", text))
    if normalize_whitespace:
        lines = [_INLINE_WS.sub(" ", ln).strip() for ln in text.split("\n")]
        text = _MANY_NL.sub("\n\n", "\n".join(lines))
    if remove_urls_emails and not normalize_whitespace and not table:
        text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def preprocess(
    doc: ParsedDoc, *, normalize_whitespace: bool, remove_urls_emails: bool
) -> ParsedDoc:
    if not normalize_whitespace and not remove_urls_emails:
        return doc
    blocks: list[Block] = []
    for b in doc.blocks:
        if b.type == "heading":
            text = clean(
                b.text,
                normalize_whitespace=normalize_whitespace,
                remove_urls_emails=False,
                table=False,
            )
        else:
            text = clean(
                b.text,
                normalize_whitespace=normalize_whitespace,
                remove_urls_emails=remove_urls_emails,
                table=b.type == "table",
            )
        if text:
            blocks.append(replace(b, text=text))
    return ParsedDoc(blocks=blocks, title=doc.title)
