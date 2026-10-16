"""解析器：文件类型识别（扩展名白名单 + 魔数）与按类型分发，统一输出 `ParsedDoc`。"""

from __future__ import annotations

from collections.abc import Callable

from aics.ingest.parsers.base import Block, ParsedDoc, ParseError, UnsupportedFileTypeError
from aics.ingest.parsers.docx import parse_docx
from aics.ingest.parsers.html import parse_html
from aics.ingest.parsers.markdown import parse_markdown
from aics.ingest.parsers.pdf import parse_pdf
from aics.ingest.parsers.text import parse_txt

FILE_TYPES = ("pdf", "docx", "md", "txt", "html")
EXTENSIONS = {
    "pdf": "pdf",
    "docx": "docx",
    "md": "md",
    "markdown": "md",
    "txt": "txt",
    "html": "html",
    "htm": "html",
}
MIME_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "md": "text/markdown",
    "txt": "text/plain",
    "html": "text/html",
}
_PARSERS: dict[str, Callable[[bytes], ParsedDoc]] = {
    "pdf": parse_pdf,
    "docx": parse_docx,
    "md": parse_markdown,
    "txt": parse_txt,
    "html": parse_html,
}


def _looks_binary(data: bytes) -> bool:
    head = data[:8192]
    return b"\x00" in head and not head.startswith((b"\xff\xfe", b"\xfe\xff"))


def detect_file_type(filename: str, data: bytes) -> str:
    """扩展名白名单 + 魔数校验；不一致或不支持 → `400 UNSUPPORTED_FILE_TYPE`。"""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    ftype = EXTENSIONS.get(ext)
    if ftype is None:
        raise UnsupportedFileTypeError(message=f"不支持的文件类型：.{ext or '(无扩展名)'}")
    ok = {
        "pdf": data.startswith(b"%PDF-"),
        "docx": data.startswith(b"PK\x03\x04") and b"word/" in data,
        "md": not _looks_binary(data),
        "txt": not _looks_binary(data),
        "html": not _looks_binary(data),
    }[ftype]
    if not ok:
        raise UnsupportedFileTypeError(message=f"文件内容与扩展名 .{ext} 不符")
    return ftype


def parse_bytes(data: bytes, file_type: str) -> ParsedDoc:
    parser = _PARSERS.get(file_type)
    if parser is None:
        raise UnsupportedFileTypeError(message=f"不支持的文件类型：{file_type}")
    if not data.strip():
        raise ParseError(message="文件为空")
    doc = parser(data)
    if not doc.blocks:
        raise ParseError(message="文档中没有可提取的文本")
    return doc


__all__ = [
    "EXTENSIONS",
    "FILE_TYPES",
    "MIME_TYPES",
    "Block",
    "ParseError",
    "ParsedDoc",
    "UnsupportedFileTypeError",
    "detect_file_type",
    "parse_bytes",
]
