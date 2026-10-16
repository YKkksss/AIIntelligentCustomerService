"""中文分词与 tsquery 构造（唯一方案，docs/03-database/01-数据库设计.md §5）。

- 每个应用一个 `jieba.Tokenizer` 实例，按 (app_id, custom_dict 哈希) LRU 缓存；
  **不修改 jieba 全局词典**（禁止 jieba.load_userdict / jieba.add_word）。
- `build_tsquery` 结果为 OR 连接的 `'词' | '词'`，供 `to_tsquery('simple', :tsq)` 使用。
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from uuid import UUID

import jieba

jieba.setLogLevel(logging.WARNING)

TOKENIZER_CACHE_SIZE = 64
MAX_QUERY_TERMS = 32

_TERM_RE = re.compile(r"^[0-9A-Za-z\u4e00-\u9fff_]+$")
_SINGLE_HAN_RE = re.compile(r"^[\u4e00-\u9fff]$")

# 停用词：只放高频虚词与客服口语，避免把 OR 查询稀释；业务词不要放这里
STOPWORDS: frozenset[str] = frozenset(
    """
    的 了 吗 呢 吧 啊 呀 哦 嗯 是 在 有 和 与 及 或 就 都 也 还 又 很 太 把 被 给 让 对 从 向 到
    我 你 他 她 它 我们 你们 他们 这 那 这个 那个 这些 那些 什么 怎么 怎么样 怎样 如何 为什么
    哪 哪里 哪些 多少 几 请问 请 一下 可以 能 能否 能不能 是否 是不是 有没有 需要 想 想要 要
    一个 一些 现在 然后 但是 因为 所以 如果 这样 那样 谢谢 您好 你好
    the a an is are of to and or in on for
    """.split()  # noqa: SIM905 - 词表按行书写便于维护
)

# Query-only additions (M2.2): interrogatives / colloquial fillers that never carry the
# answer. Applied to questions only (the index keeps every token), so adding words here
# needs no rebuild. Quantity/limit words (以内、以上、最多) stay: they often anchor rules.
QUESTION_WORDS: frozenset[str] = frozenset(
    """
    多久 多长 多长时间 多大 多远 多快 几天 几个 几次 几种 几号 几点 是多少 有多少 要多久
    怎么办 怎么弄 怎么回事 怎么处理 咋 咋办 啥 啥时候 什么样 什么时候 哪个 哪种 哪家 哪儿
    么 嘛 呗 吗 告诉 知道 问 问一下 咨询 大概 大约 具体 到底 究竟 应该 得 会 会不会
    可不可以 行不行 好不好 算不算 是不是 能够 还是 可能 帮 帮我 麻烦
    what how which when where who whom why whose do does did can could would should will
    i me my we our you your it its be been this that these those with please there any
    """.split()  # noqa: SIM905 - one line per theme for maintenance
)
QUERY_STOPWORDS: frozenset[str] = STOPWORDS | QUESTION_WORDS

_lock = threading.Lock()
_cache: OrderedDict[tuple[str, str], jieba.Tokenizer] = OrderedDict()


def _dict_key(custom_dict: Iterable[str]) -> tuple[str, list[str]]:
    words = sorted({w.strip() for w in custom_dict if w and w.strip()})
    digest = hashlib.sha256("\n".join(words).encode("utf-8")).hexdigest()[:16]
    return digest, words


def get_tokenizer(app_id: UUID | str, custom_dict: Sequence[str] = ()) -> jieba.Tokenizer:
    """取该应用的分词器；custom_dict 变化会得到新实例（旧实例随 LRU 淘汰）。"""
    digest, words = _dict_key(custom_dict)
    key = (str(app_id), digest)
    with _lock:
        tok = _cache.get(key)
        if tok is not None:
            _cache.move_to_end(key)
            return tok
    tok = jieba.Tokenizer()
    for w in words:
        tok.add_word(w)  # 实例方法，只影响本应用的分词器
    with _lock:
        _cache[key] = tok
        _cache.move_to_end(key)
        while len(_cache) > TOKENIZER_CACHE_SIZE:
            _cache.popitem(last=False)
    return tok


def clear_tokenizer_cache() -> None:
    with _lock:
        _cache.clear()


def tokenize_for_index(app_id: UUID | str, text: str, custom_dict: Sequence[str] = ()) -> str:
    """入库用：lcut_for_search 后空格连接，写入 search_text（tsv 由生成列计算）。"""
    tok = get_tokenizer(app_id, custom_dict)
    return " ".join(t for t in (w.strip() for w in tok.lcut_for_search(text)) if t)


def _expand(
    tok: jieba.Tokenizer, question: str, stopwords: frozenset[str], keep: set[str]
) -> Iterable[str]:
    for seg in tok.lcut(question):
        w = seg.strip().lower()
        if not w or (w in stopwords and w not in keep):
            continue
        subs = tok.lcut_for_search(seg)
        yield from subs if seg in subs else [*subs, seg]


def build_tsquery(
    app_id: UUID | str,
    question: str,
    custom_dict: Sequence[str] = (),
    *,
    stopwords: frozenset[str] = QUERY_STOPWORDS,
) -> str | None:
    """构造 OR 语义 tsquery 字符串；无可用词返回 None（调用方跳过关键词路，不记 degraded）。

    `stopwords` defaults to QUERY_STOPWORDS (question words stripped, M2.2); the parameter
    exists for evaluation baselines and must not be overridden in production paths.
    Custom-dict words are never dropped, even when they collide with a stopword.
    """
    keep = {w.strip().lower() for w in custom_dict if w and w.strip()}
    tok = get_tokenizer(app_id, custom_dict)
    terms: list[str] = []
    seen: set[str] = set()
    # Stopwords are judged on precise-mode segments, then each kept segment is expanded with
    # its search-mode sub-words (matching the index side). Filtering search-mode output
    # directly would leak fragments of dropped phrases (多长时间 → 长时间 / 时间).
    for raw in _expand(tok, question or "", stopwords, keep):
        w = raw.strip().lower()
        if not w or not _TERM_RE.match(w):
            continue  # 去掉 tsquery 运算符、引号、标点
        if w not in keep and (w in stopwords or _SINGLE_HAN_RE.match(w)):
            continue
        if w in seen:
            continue
        seen.add(w)
        terms.append(w)
        if len(terms) >= MAX_QUERY_TERMS:
            break
    if not terms:
        return None
    # 词已限定为 [0-9A-Za-z\u4e00-\u9fff_]，不含单引号与反斜杠，可直接包裹
    return " | ".join(f"'{t}'" for t in terms)
