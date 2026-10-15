"""全系统唯一的 token 计数（估算口径 tiktoken cl100k_base）。

用于切块长度、chunks.tokens、messages.token_count、上下文预算、80% 窗口校验；
计费用供应商返回的 usage，不用这里的估算（docs/02-architecture/04-文档处理流水线.md §7）。
"""

from __future__ import annotations

from functools import lru_cache

import tiktoken

from aics.core.config import TOKEN_ENCODING


@lru_cache(maxsize=1)
def _encoding() -> tiktoken.Encoding:
    return tiktoken.get_encoding(TOKEN_ENCODING)


def count_tokens(text: str) -> int:
    if not text:
        return 0
    # disallowed_special=() 让 "<|endoftext|>" 等按普通文本计数，用户内容里出现也不报错
    return len(_encoding().encode(text, disallowed_special=()))
