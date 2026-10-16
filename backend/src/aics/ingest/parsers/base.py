"""解析结果的统一结构（文档处理流水线 §3.2）。"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from aics.core.errors import AppError

BlockType = Literal["heading", "paragraph", "table", "list"]


@dataclass(frozen=True)
class Block:
    type: BlockType
    text: str
    level: int | None = None  # 仅 heading：1–6
    page: int | None = None  # 仅 PDF：从 1 开始


@dataclass
class ParsedDoc:
    blocks: list[Block] = field(default_factory=list)
    title: str | None = None  # 文档内推断的标题（HTML <title>、PDF 元数据等），可为空

    def to_dict(self) -> dict[str, Any]:
        return {"title": self.title, "blocks": [asdict(b) for b in self.blocks]}


class ParseError(AppError):
    """解析失败：message 为用户可读原因，写入 documents.error。"""

    code, status, message = "PARSE_FAILED", 422, "文档解析失败"


class UnsupportedFileTypeError(AppError):
    code, status, message = "UNSUPPORTED_FILE_TYPE", 400, "不支持的文件类型"


_WS = re.compile(r"[ \t\u00a0\u3000]+")


def _is_control(ch: str) -> bool:
    o = ord(ch)
    return o < 32 or 0x7F <= o <= 0x9F


def clean_text(text: str) -> str:
    """统一空白：去控制字符、合并行内空白、去首尾空白；保留换行。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    # 去全部 C0/C1 控制字符（含 DEL 0x7F、0x80–0x9F），只保留换行与制表符
    text = "".join(ch for ch in text if ch in "\n\t" or not _is_control(ch))
    lines = [_WS.sub(" ", ln).strip() for ln in text.split("\n")]
    return "\n".join(lines).strip()


def table_to_markdown(rows: list[list[str]]) -> str:
    """二维表 → Markdown 表（首行作表头）；单元格内换行与竖线转义。"""
    rows = [[clean_text(c or "").replace("\n", " ").replace("|", "\\|") for c in r] for r in rows]
    rows = [r for r in rows if any(r)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)
