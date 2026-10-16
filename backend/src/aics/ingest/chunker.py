"""切块（文档处理流水线 §3.3）。

通用规则：
- 维护标题栈得到 `heading_path`（`A > B > C`）；`split_by_heading=true` 时标题变化即章节边界，切块与重叠都不跨章节。
- 单元（段落 / 列表 / 表格）顺序累加，切块 token 数 ≤ 上限（计数一律 `core.tokens.count_tokens`）。
- 超长段落按句末标点（。！？!?；;）与换行切句，单句仍超长按 token 二分硬切。
- 自定义分隔符（字面量，不是正则）先把段落切成更小的单元，再参与累加。
- 表格（`keep_tables=true`）尽量整块不拆：超过上限时按行拆分并在每块重复表头；表格不作为重叠来源。
- 重叠：新块以前一块末尾不超过 `chunk_overlap` token 的完整句子开头；加上重叠会超过上限时缩减重叠（仍只保留完整句子，一句都放不下则本块不重叠）。
- 标题本身不写入正文（向量化文本为 `heading_path + "\\n" + text`，见 §3.6）。

策略（`strategy`）：heading 通用；article 法规（第X条为单位、编/章/节进标题路径）；section 论文（摘要、参考文献独立）；
faq 问答对（一问一块，问题随每块重复）；clause 合同（顶级条款为单位）。
父子模式（`mode=parent_child`）：先按策略以 `parent_size`、无重叠切出父块，再把每个父块切成 `child_size` / `child_overlap`
的子块；子块用于向量化与检索，父块正文送入 LLM（见 RAG 流程设计 §4.1）。
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, fields
from typing import Any, Literal

from aics.core.tokens import count_tokens
from aics.ingest.parsers.base import Block, ParsedDoc
from aics.ingest.preprocess import preprocess

HEADING_SEP = " > "
_SENT = re.compile(r"(?<=[。！？!?；;])|\n")
_CN_NUM = "零〇一二三四五六七八九十百千万两0-9０-９"
_ARTICLE = re.compile(rf"^第[{_CN_NUM}]+条")
_ARTICLE_SPLIT = re.compile(rf"(?:(?<=^)|(?<=\n)|(?<=[。；;]))\s*(?=第[{_CN_NUM}]+条[\s　])")
_LEGAL_HEADING = re.compile(rf"^第[{_CN_NUM}]+(编|分编|章|节)(?:[\s　]+\S.{{0,38}})?$")
_LEGAL_LEVEL = {"编": 2, "分编": 3, "章": 4, "节": 5}
_CLAUSE_TOP = re.compile(
    r"^(第[" + _CN_NUM + r"]+条|[一二三四五六七八九十]+[、.．]|\d{1,3}[、.．](?!\d))\s*"
)
_CLAUSE_SUB = re.compile(r"^\d{1,3}(?:[.．]\d{1,3})+[、.．]?\s*")
_QUESTION = re.compile(r"^(Q|q|问|问题)\s*\d*\s*[:：.、]")
_ABSTRACT = re.compile(r"^(摘\s*要|abstract|关键词|key\s*words)\s*[:：]\s*", re.I)
_REFERENCES = re.compile(r"^(参考文献|references|bibliography)\s*$", re.I)
_END_PUNCT = ("。", "；", ";", "！", "？", "!", "?", "，", ",")

Strategy = Literal["heading", "article", "section", "faq", "clause"]


@dataclass(frozen=True)
class ChunkOptions:
    """切块参数；字段与 `apps.chunk_config` 同名（由调用方从 `ChunkConfig` 转换，未知键忽略）。"""

    strategy: Strategy = "heading"
    chunk_size: int = 500
    chunk_overlap: int = 80
    separators: tuple[str, ...] = ()
    split_by_heading: bool = True
    keep_tables: bool = True
    mode: Literal["general", "parent_child"] = "general"
    parent_mode: Literal["paragraph", "full_doc"] = "paragraph"
    parent_size: int = 1024
    full_doc_max_tokens: int = 10000
    child_size: int = 200
    child_overlap: int = 20
    child_separators: tuple[str, ...] = ()
    normalize_whitespace: bool = True
    remove_urls_emails: bool = False

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> ChunkOptions:
        names = {f.name for f in fields(cls)}
        kw = {k: v for k, v in data.items() if k in names}
        for key in ("separators", "child_separators"):
            if key in kw:
                kw[key] = tuple(kw[key])
        return cls(**kw)

    def validate(self) -> None:
        def pair(size: int, overlap: int) -> None:
            if size <= 0 or overlap < 0 or overlap * 2 > size:
                raise ValueError("切块大小 / 重叠不合法")

        pair(self.chunk_size, self.chunk_overlap)
        if self.mode == "parent_child":
            pair(self.child_size, self.child_overlap)
            if self.parent_mode == "paragraph" and self.child_size >= self.parent_size:
                raise ValueError("child_size 必须小于 parent_size")


@dataclass(frozen=True)
class ChunkDraft:
    ordinal: int
    heading_path: str
    text: str
    tokens: int
    page_from: int | None
    page_to: int | None
    overlap_tokens: int = 0  # 开头来自上一块的重叠部分 token 数
    parent_ordinal: int | None = None  # 父子模式：所属父块序号


@dataclass(frozen=True)
class ParentDraft:
    ordinal: int
    heading_path: str
    text: str
    tokens: int
    page_from: int | None
    page_to: int | None


@dataclass
class ChunkResult:
    chunks: list[ChunkDraft]  # 可检索单元（父子模式下为子块）
    parents: list[ParentDraft] = field(default_factory=list)  # 仅父子模式


# ------------------------------------------------------------------ 事件流
@dataclass
class _Unit:
    text: str
    page: int | None
    is_table: bool = False
    overlap: bool = False
    path: str = ""


@dataclass(frozen=True)
class _Heading:
    level: int
    text: str
    page: int | None = None
    no_overlap_section: bool = False  # 如参考文献：章节内不重叠


@dataclass(frozen=True)
class _Break:
    """硬边界：结束当前块，不带重叠（法规的条、合同的顶级条款、FAQ 的问题）。"""

    prefix: str | None = None  # 之后每块都以此开头（FAQ 的问题）
    page: int | None = None


_Event = _Heading | _Break | _Unit


def _split_separators(text: str, seps: tuple[str, ...]) -> list[str]:
    if not seps:
        return [text]
    pattern = "|".join(re.escape(s) for s in sorted(seps, key=len, reverse=True))
    return [p.strip() for p in re.split(pattern, text) if p.strip()]


def _events(
    doc: ParsedDoc, o: ChunkOptions, separators: tuple[str, ...] | None = None
) -> Iterator[_Event]:
    seps = o.separators if separators is None else separators
    for blk in doc.blocks:
        if blk.type == "heading":
            yield from _heading_events(blk, o)
            continue
        if blk.type == "table":
            yield _Unit(blk.text, blk.page, is_table=o.keep_tables)
            continue
        for piece in _split_separators(blk.text, seps):
            yield from _text_events(piece, blk, o)


def _heading_events(blk: Block, o: ChunkOptions) -> Iterator[_Event]:
    text = blk.text
    if o.strategy == "faq" and (text.endswith(("？", "?")) or _QUESTION.match(text)):
        yield _Break(prefix=text, page=blk.page)
        return
    if o.strategy == "article":
        m = _LEGAL_HEADING.match(text)
        if m:
            yield _Heading(_LEGAL_LEVEL[m.group(1)], text, blk.page)
            return
    yield _Heading(blk.level or 1, text, blk.page, bool(_REFERENCES.match(text)))


def _text_events(text: str, blk: Block, o: ChunkOptions) -> Iterator[_Event]:
    page = blk.page
    if o.strategy == "article":
        for piece in (p.strip() for p in _ARTICLE_SPLIT.split(text)):
            if not piece:
                continue
            m = _LEGAL_HEADING.match(piece)
            if m:
                yield _Heading(_LEGAL_LEVEL[m.group(1)], piece, page)
            elif _ARTICLE.match(piece):
                yield _Break(page=page)
                yield _Unit(piece, page)
            else:
                yield _Unit(piece, page)
        return
    if o.strategy == "clause":
        for line in (ln.strip() for ln in text.split("\n")):
            if not line:
                continue
            if _CLAUSE_TOP.match(line) and not _CLAUSE_SUB.match(line):
                if len(line) <= 30 and not line.endswith(_END_PUNCT):
                    yield _Heading(2, line, page)
                else:
                    yield _Break(page=page)
                    yield _Unit(line, page)
            else:
                yield _Unit(line, page)
        return
    if o.strategy == "faq" and _QUESTION.match(text):
        q, _, rest = text.partition("\n")
        yield _Break(prefix=q.strip(), page=page)
        if rest.strip():
            yield _Unit(rest.strip(), page)
        return
    if o.strategy == "section":
        m = _ABSTRACT.match(text)
        if m and m.group(1).replace(" ", "").lower() in ("摘要", "abstract"):
            yield _Heading(2, m.group(1).replace(" ", ""), page)
            if text[m.end() :].strip():
                yield _Unit(text[m.end() :].strip(), page)
            return
    yield _Unit(text, page)


# ------------------------------------------------------------------ 累加器
@dataclass
class _Builder:
    size: int
    overlap_budget: int
    out: list[ChunkDraft] = field(default_factory=list)
    heading_path: str = ""
    units: list[_Unit] = field(default_factory=list)
    overlap_text: str = ""
    prefix: str | None = None
    section_no_overlap: bool = False

    @property
    def _overlap_on(self) -> bool:
        return self.overlap_budget > 0 and self.prefix is None and not self.section_no_overlap

    def section(self, heading_path: str, *, no_overlap: bool = False) -> None:
        self.flush(keep_overlap=False)
        self.heading_path = heading_path
        self.prefix = None
        self.section_no_overlap = no_overlap

    def hard_break(self, prefix: str | None) -> None:
        self.flush(keep_overlap=False)
        self.prefix = prefix

    def add(self, unit: _Unit) -> None:
        unit.path = unit.path or self.heading_path
        for piece in self._fit(unit):
            if self._body() and count_tokens(self._join([*self._with_prefix(), piece])) > self.size:
                self.flush(keep_overlap=True)
                self._prepend_overlap(piece)
            elif not self._body() and self.overlap_text:
                self._prepend_overlap(piece)
            self.units.append(piece)

    def flush(self, *, keep_overlap: bool) -> None:
        body = [u for u in self.units if u.text]
        if any(not u.overlap for u in body):
            full = self._with_prefix()
            text = self._join(full)
            pages = [u.page for u in full if u.page is not None]
            first = next(u for u in body if not u.overlap)
            self.out.append(
                ChunkDraft(
                    ordinal=len(self.out),
                    heading_path=first.path or self.heading_path,
                    text=text,
                    tokens=count_tokens(text),
                    page_from=min(pages) if pages else None,
                    page_to=max(pages) if pages else None,
                    overlap_tokens=sum(count_tokens(u.text) for u in body if u.overlap),
                )
            )
            self.overlap_text = self._tail(body) if keep_overlap and self._overlap_on else ""
        elif not keep_overlap:
            self.overlap_text = ""
        self.units = []

    # ---------------- 内部
    def _body(self) -> list[_Unit]:
        return self.units

    def _with_prefix(self) -> list[_Unit]:
        if self.prefix is None:
            return list(self.units)
        page = self.units[0].page if self.units else None
        return [_Unit(self.prefix, page), *self.units]

    @staticmethod
    def _join(units: list[_Unit]) -> str:
        return "\n".join(u.text for u in units)

    def _limit(self) -> int:
        return self.size - (count_tokens(self.prefix) + 1 if self.prefix else 0)

    def _prepend_overlap(self, nxt: _Unit) -> None:
        ov = self.overlap_text
        self.overlap_text = ""
        if not ov or not self._overlap_on:
            return
        budget = self._limit() - count_tokens(nxt.text) - 1
        if budget <= 0:
            return
        ov = _sentence_suffix_within(ov, min(self.overlap_budget, budget))
        if ov:
            self.units.append(_Unit(ov, nxt.page, overlap=True, path=nxt.path))

    def _tail(self, body: list[_Unit]) -> str:
        src = [u for u in body if not u.is_table and not u.overlap]
        if not src or body[-1].is_table:
            return ""
        sents = _sentences(src[-1].text)
        picked: list[str] = []
        for s in reversed(sents):
            if count_tokens("".join([s, *picked])) > self.overlap_budget:
                break
            picked.insert(0, s)
        if picked:
            return "".join(picked).strip()
        return _suffix_within(sents[-1], self.overlap_budget)

    def _fit(self, unit: _Unit) -> list[_Unit]:
        limit = max(1, self._limit())
        if count_tokens(unit.text) <= limit:
            return [unit]
        if unit.is_table:
            return [
                _Unit(t, unit.page, True, path=unit.path) for t in _split_table(unit.text, limit)
            ]
        pieces: list[_Unit] = []
        cur = ""
        for s in _sentences(unit.text):
            for part in _hard_split(s, limit):
                cand = cur + part
                if cur and count_tokens(cand) > limit:
                    pieces.append(_Unit(cur.strip(), unit.page, path=unit.path))
                    cur = part
                else:
                    cur = cand
        if cur.strip():
            pieces.append(_Unit(cur.strip(), unit.page, path=unit.path))
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


def _sentence_suffix_within(text: str, budget: int) -> str:
    """缩减重叠时只保留末尾的完整句子；一句都放不下就不重叠（避免新块以半句话开头）。"""
    if budget <= 0:
        return ""
    if count_tokens(text) <= budget:
        return text.strip()
    picked: list[str] = []
    for s in reversed(_sentences(text)):
        if count_tokens("".join([s, *picked])) > budget:
            break
        picked.insert(0, s)
    return "".join(picked).strip()


def _suffix_within(text: str, budget: int) -> str:
    if budget <= 0:
        return ""
    if count_tokens(text) <= budget:
        return text.strip()
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi) // 2
        if count_tokens(text[mid:]) <= budget:
            hi = mid
        else:
            lo = mid + 1
    return text[lo:].strip()


def _is_sep_row(line: str) -> bool:
    return bool(line) and set(line.replace("|", "").strip()) <= set("-: ")


def _split_table(md: str, size: int) -> list[str]:
    lines = md.split("\n")
    header = lines[:2] if len(lines) >= 2 and _is_sep_row(lines[1]) else lines[:1]
    rows = lines[len(header) :]
    out: list[str] = []
    cur: list[str] = []
    for row in rows:
        for part in _hard_split(row, max(1, size - count_tokens("\n".join(header)) - 1)):
            if cur and count_tokens("\n".join([*header, *cur, part])) > size:
                out.append("\n".join([*header, *cur]))
                cur = [part]
            else:
                cur.append(part)
    if cur:
        out.append("\n".join([*header, *cur]))
    return out or [md]


# ------------------------------------------------------------------ 驱动
def _run(events: list[_Event], size: int, overlap: int, split_by_heading: bool) -> list[ChunkDraft]:
    b = _Builder(size=size, overlap_budget=overlap)
    stack: list[tuple[int, str]] = []
    for ev in events:
        if isinstance(ev, _Heading):
            while stack and stack[-1][0] >= ev.level:
                stack.pop()
            stack.append((ev.level, ev.text))
            path = HEADING_SEP.join(t for _, t in stack)
            if split_by_heading:
                b.section(path, no_overlap=ev.no_overlap_section)
            else:
                b.heading_path = path
                b.add(_Unit(ev.text, ev.page, path=path))
        elif isinstance(ev, _Break):
            b.hard_break(ev.prefix)
        else:
            b.add(_Unit(ev.text, ev.page, ev.is_table, path=b.heading_path))
    b.flush(keep_overlap=False)
    return b.out


def _children_events(parent: ChunkDraft, seps: tuple[str, ...] = ()) -> list[_Event]:
    """父块正文 → 子块单元：连续的表格行合为一个表格单元，其余按行（再按子块分隔符切）。"""
    events: list[_Event] = []
    table: list[str] = []
    for line in parent.text.split("\n"):
        if line.startswith("|"):
            table.append(line)
            continue
        if table:
            events.append(_Unit("\n".join(table), parent.page_from, is_table=True))
            table = []
        for piece in _split_separators(line, seps) if line.strip() else []:
            events.append(_Unit(piece, parent.page_from))
    if table:
        events.append(_Unit("\n".join(table), parent.page_from, is_table=True))
    return events


def _strip_overlap(c: ChunkDraft) -> str:
    """去掉子块开头来自上一块的重叠行，用于拼回父块正文。"""
    if c.overlap_tokens and "\n" in c.text:
        return c.text.split("\n", 1)[1]
    return c.text


def _full_doc(doc: ParsedDoc, o: ChunkOptions) -> ChunkResult:
    """全文父块：先按章节切子块（标题路径准确），再把连续子块装入 ≤ full_doc_max_tokens 的父块。

    与 Dify 截断超长部分不同：超过上限时顺序开新的父块，内容不丢；子块不跨父块，父块内章节变化处插入标题路径行。
    """
    seps = o.child_separators or o.separators
    children = _run(list(_events(doc, o, seps)), o.child_size, o.child_overlap, True)
    parents: list[ParentDraft] = []
    chunks: list[ChunkDraft] = []
    cur: list[ChunkDraft] = []
    cur_lines: list[str] = []

    def lines_for(c: ChunkDraft, prev_path: str | None) -> list[str]:
        head = [c.heading_path] if c.heading_path and c.heading_path != prev_path else []
        return [*head, _strip_overlap(c)]

    def close() -> None:
        if not cur:
            return
        text = "\n".join(cur_lines)
        pages = [p for c in cur for p in (c.page_from, c.page_to) if p is not None]
        po = len(parents)
        parents.append(
            ParentDraft(
                po,
                cur[0].heading_path,
                text,
                count_tokens(text),
                min(pages) if pages else None,
                max(pages) if pages else None,
            )
        )
        for i, c in enumerate(cur):
            ov = c.overlap_tokens if i else 0  # 父块内第一个子块不带跨父块重叠
            chunks.append(
                ChunkDraft(
                    len(chunks),
                    c.heading_path,
                    c.text if i else _strip_overlap(c),
                    c.tokens if i else count_tokens(_strip_overlap(c)),
                    c.page_from,
                    c.page_to,
                    ov,
                    po,
                )
            )
        cur.clear()
        cur_lines.clear()

    for c in children:
        prev = cur[-1].heading_path if cur else None
        add = lines_for(c, prev)
        if cur and count_tokens("\n".join([*cur_lines, *add])) > o.full_doc_max_tokens:
            close()
            add = lines_for(c, None)
        cur.append(c)
        cur_lines.extend(add)
    close()
    return ChunkResult(chunks=chunks, parents=parents)


def split_document(doc: ParsedDoc, options: ChunkOptions | None = None) -> ChunkResult:
    """预处理 → 按策略切块（→ 父子模式）。入库与预览共用此入口，保证预览结果与入库一致。"""
    o = options or ChunkOptions()
    o.validate()
    doc = preprocess(
        doc, normalize_whitespace=o.normalize_whitespace, remove_urls_emails=o.remove_urls_emails
    )
    if o.mode == "parent_child" and o.parent_mode == "full_doc":
        return _full_doc(doc, o)
    events = list(_events(doc, o))
    if o.mode != "parent_child":
        return ChunkResult(chunks=_run(events, o.chunk_size, o.chunk_overlap, o.split_by_heading))

    parent_drafts = _run(events, o.parent_size, 0, o.split_by_heading)
    parents: list[ParentDraft] = []
    chunks: list[ChunkDraft] = []
    for p in parent_drafts:
        parents.append(
            ParentDraft(p.ordinal, p.heading_path, p.text, p.tokens, p.page_from, p.page_to)
        )
        for c in _run(_children_events(p, o.child_separators), o.child_size, o.child_overlap, True):
            chunks.append(
                ChunkDraft(
                    ordinal=len(chunks),
                    heading_path=p.heading_path,
                    text=c.text,
                    tokens=c.tokens,
                    page_from=p.page_from,
                    page_to=p.page_to,
                    overlap_tokens=c.overlap_tokens,
                    parent_ordinal=p.ordinal,
                )
            )
    return ChunkResult(chunks=chunks, parents=parents)


def chunk_document(
    doc: ParsedDoc, *, chunk_size: int = 500, chunk_overlap: int = 80
) -> list[ChunkDraft]:
    """通用策略的便捷入口（等价于 `split_document(doc, ChunkOptions(chunk_size=…, chunk_overlap=…)).chunks`）。"""
    return split_document(
        doc, ChunkOptions(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    ).chunks
