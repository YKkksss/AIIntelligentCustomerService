"""切块（文档处理流水线 §3.3）。

规则：
- 维护标题栈得到 `heading_path`（`A > B > C`）；标题变化即章节边界，切块不跨章节，重叠也不跨章节。
- 章节内按顺序累加单元（段落 / 列表 / 表格），使切块 token 数 ≤ `chunk_size`（计数一律 `core.tokens.count_tokens`）。
- 超长段落按句末标点（。！？!?；;）与换行切成句子，单句仍超长按 token 二分硬切。
- 表格尽量整块不拆：能放进当前块就放，否则单独成块；表格本身超过 `chunk_size` 时按行拆分，每块重复表头。
- 重叠：新块以前一块末尾不超过 `chunk_overlap` token 的完整句子开头（单句超过重叠预算时取其末尾片段）；
  表格不作为重叠来源；加上重叠会超过 `chunk_size` 时缩减重叠。
- 标题本身不写入正文（向量化文本为 `heading_path + "\n" + text`，见 §3.6）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from aics.core.tokens import count_tokens
from aics.ingest.parsers.base import ParsedDoc

HEADING_SEP = " > "
_SENT = re.compile(r"(?<=[。！？!?；;])|\n")


@dataclass(frozen=True)
class ChunkDraft:
    ordinal: int
    heading_path: str
    text: str
    tokens: int
    page_from: int | None
    page_to: int | None
    overlap_tokens: int = 0  # 开头来自上一块的重叠部分 token 数


@dataclass
class _Unit:
    text: str
    page: int | None
    is_table: bool = False
    overlap: bool = False


@dataclass
class _Builder:
    size: int
    overlap_budget: int
    out: list[ChunkDraft] = field(default_factory=list)
    heading_path: str = ""
    units: list[_Unit] = field(default_factory=list)
    overlap_text: str = ""

    # ---------------- 公共
    def section(self, heading_path: str) -> None:
        self.flush(keep_overlap=False)
        self.heading_path = heading_path

    def add(self, unit: _Unit) -> None:
        for piece in self._fit(unit):
            if self.units and count_tokens(self._join([*self.units, piece])) > self.size:
                self.flush(keep_overlap=True)
                self._prepend_overlap(piece)
            elif not self.units and self.overlap_text:
                self._prepend_overlap(piece)
            self.units.append(piece)

    def flush(self, *, keep_overlap: bool) -> None:
        body = [u for u in self.units if u.text]
        if body and any(not u.overlap for u in body):
            text = self._join(body)
            pages = [u.page for u in body if u.page is not None]
            ov = sum(count_tokens(u.text) for u in body if u.overlap)
            self.out.append(
                ChunkDraft(
                    ordinal=len(self.out),
                    heading_path=self.heading_path,
                    text=text,
                    tokens=count_tokens(text),
                    page_from=min(pages) if pages else None,
                    page_to=max(pages) if pages else None,
                    overlap_tokens=ov,
                )
            )
            self.overlap_text = self._tail(body) if keep_overlap else ""
        elif not keep_overlap:
            self.overlap_text = ""
        self.units = []

    # ---------------- 内部
    @staticmethod
    def _join(units: list[_Unit]) -> str:
        return "\n".join(u.text for u in units)

    def _prepend_overlap(self, nxt: _Unit) -> None:
        ov = self.overlap_text
        self.overlap_text = ""
        if not ov:
            return
        budget = self.size - count_tokens(nxt.text) - 1
        if budget <= 0:
            return
        ov = _suffix_within(ov, min(self.overlap_budget, budget))
        if ov:
            self.units.append(_Unit(ov, nxt.page, overlap=True))

    def _tail(self, body: list[_Unit]) -> str:
        """前一块末尾 ≤ overlap token 的完整句子（表格与重叠单元不参与）。"""
        if self.overlap_budget <= 0:
            return ""
        src = [u for u in body if not u.is_table and not u.overlap]
        if not src or body[-1].is_table:
            return ""
        sents = _sentences(src[-1].text)
        picked: list[str] = []
        for s in reversed(sents):
            cand = "".join([s, *picked])
            if count_tokens(cand) > self.overlap_budget:
                break
            picked.insert(0, s)
        if picked:
            return "".join(picked).strip()
        return _suffix_within(sents[-1], self.overlap_budget) if sents else ""

    def _fit(self, unit: _Unit) -> list[_Unit]:
        if count_tokens(unit.text) <= self.size:
            return [unit]
        if unit.is_table:
            return [_Unit(t, unit.page, True) for t in _split_table(unit.text, self.size)]
        pieces: list[_Unit] = []
        cur = ""
        for s in _sentences(unit.text):
            for part in _hard_split(s, self.size):
                cand = cur + part
                if cur and count_tokens(cand) > self.size:
                    pieces.append(_Unit(cur.strip(), unit.page))
                    cur = part
                else:
                    cur = cand
        if cur.strip():
            pieces.append(_Unit(cur.strip(), unit.page))
        return pieces


def _sentences(text: str) -> list[str]:
    parts = [p for p in _SENT.split(text) if p and p.strip()]
    return parts or [text]


def _hard_split(text: str, size: int) -> list[str]:
    """单句超过 size：按字符二分找到不超过 size token 的最长前缀，循环切分。"""
    out: list[str] = []
    rest = text
    while rest and count_tokens(rest) > size:
        lo, hi = 1, len(rest)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if count_tokens(rest[:mid]) <= size:
                lo = mid
            else:
                hi = mid - 1
        out.append(rest[:lo])
        rest = rest[lo:]
    if rest:
        out.append(rest)
    return out


def _suffix_within(text: str, budget: int) -> str:
    if budget <= 0:
        return ""
    if count_tokens(text) <= budget:
        return text.strip()
    lo, hi = 0, len(text)
    while lo < hi:  # 找最短起点 i，使 text[i:] ≤ budget
        mid = (lo + hi) // 2
        if count_tokens(text[mid:]) <= budget:
            hi = mid
        else:
            lo = mid + 1
    return text[lo:].strip()


def _split_table(md: str, size: int) -> list[str]:
    lines = md.split("\n")
    header = (
        lines[:2]
        if len(lines) >= 2 and set(lines[1].replace("|", "").strip()) <= set("-: ")
        else lines[:1]
    )
    rows = lines[len(header) :]
    out: list[str] = []
    cur: list[str] = []
    for row in rows:
        for part in _hard_split(row, max(1, size - count_tokens("\n".join(header)) - 1)):
            cand = "\n".join([*header, *cur, part])
            if cur and count_tokens(cand) > size:
                out.append("\n".join([*header, *cur]))
                cur = [part]
            else:
                cur.append(part)
    if cur:
        out.append("\n".join([*header, *cur]))
    return out or [md]


def chunk_document(
    doc: ParsedDoc, *, chunk_size: int = 500, chunk_overlap: int = 80
) -> list[ChunkDraft]:
    if chunk_size <= 0 or chunk_overlap < 0 or chunk_overlap * 2 > chunk_size:
        raise ValueError("chunk_size / chunk_overlap 不合法")
    b = _Builder(size=chunk_size, overlap_budget=chunk_overlap)
    stack: list[tuple[int, str]] = []
    for blk in doc.blocks:
        if blk.type == "heading":
            level = blk.level or 1
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, blk.text))
            b.section(HEADING_SEP.join(t for _, t in stack))
        else:
            b.add(_Unit(blk.text, blk.page, blk.type == "table"))
    b.flush(keep_overlap=False)
    return b.out
