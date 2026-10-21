"""64-bit SimHash for near-duplicate document hints (M4.3b, 设计讨论 0010 §2.4).

SimHash 的性质：文档改几句话，指纹只有少数几位翻转；内容不同则约有半数位不同。所以入库时
把新文档与同应用已有文档比较海明距离——`<= NEAR_DUPLICATE_BITS` 提示「高度相似」，只提示不阻
止（完全相同的正文仍走既有的 `content_hash` 去重）。

分词口径：中文按字切 3-gram（不足 3 字时取整个片段），ASCII 按小写单词。不引入分词依赖，保
证同一正文在任何进程 / 任何机器上得到同一个 64 位整数，测试可直接断言距离。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Iterator

CJK = re.compile(r"[\u4e00-\u9fff]")
WORD = re.compile(r"[0-9a-z]+")
NEAR_DUPLICATE_BITS = 3
SHINGLE = 3


def tokens_of(text: str) -> list[str]:
    """把正文切成 SimHash 的特征集：中文 3-gram + ASCII 小写单词。"""
    out: list[str] = []
    cjk_run: list[str] = []
    word_run: list[str] = []

    def flush_words() -> None:
        if word_run:
            out.extend(WORD.findall("".join(word_run)))
            word_run.clear()

    def flush_cjk() -> None:
        if not cjk_run:
            return
        run = cjk_run[:]
        if len(run) <= SHINGLE:
            out.append("".join(run))
        else:
            out.extend("".join(run[i : i + SHINGLE]) for i in range(len(run) - SHINGLE + 1))
        cjk_run.clear()

    for ch in text:
        if CJK.match(ch):
            flush_words()
            cjk_run.append(ch)
        else:
            flush_cjk()
            word_run.append(ch.lower())
    flush_cjk()
    flush_words()
    return out


def _bit_weights(tokens: Iterable[str]) -> Iterator[tuple[int, str]]:
    for token in tokens:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        yield int.from_bytes(digest, "big"), token


def simhash(text: str) -> int:
    """正文的 64 位指纹；空正文返回 0（调用方据此跳过比较）。"""
    tokens = tokens_of(text)
    if not tokens:
        return 0
    votes = [0] * 64
    for value, _ in _bit_weights(tokens):
        for bit in range(64):
            votes[bit] += 1 if value >> bit & 1 else -1
    result = 0
    for bit, vote in enumerate(votes):
        if vote > 0:
            result |= 1 << bit
    return result


def hamming(left: int, right: int) -> int:
    """两个指纹不同的位数（0 = 完全相同，64 = 完全相反）。"""
    return (left ^ right).bit_count()


def near_duplicate(left: int, right: int) -> bool:
    return left != 0 and right != 0 and hamming(left, right) <= NEAR_DUPLICATE_BITS
