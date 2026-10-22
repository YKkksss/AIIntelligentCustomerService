"""文件存储接口。key 由服务端生成（`{app_id}/{document_id}/v{version}.{ext}`），不用用户文件名做路径。"""

from __future__ import annotations

import re
from typing import Protocol, runtime_checkable
from uuid import UUID

_EXT_RE = re.compile(r"^[a-z0-9]{1,10}$")


class StorageError(Exception):
    pass


class InvalidKeyError(StorageError, ValueError):
    pass


class ObjectNotFoundError(StorageError, FileNotFoundError):
    pass


def document_key(app_id: UUID, document_id: UUID, version: int, ext: str) -> str:
    """生效版本文件的存储 key（数据库设计 documents.storage_key）。"""
    ext = ext.lower().lstrip(".")
    if not _EXT_RE.match(ext):
        raise InvalidKeyError(f"非法扩展名：{ext!r}")
    if version < 1:
        raise InvalidKeyError("version 必须 ≥ 1")
    return f"{UUID(str(app_id))}/{UUID(str(document_id))}/v{version}.{ext}"


def kb_document_key(kb_id: UUID, document_id: UUID, version: int, ext: str) -> str:
    """知识库文档的存储 key（M4.7 ⑤：文档归属库，前缀用 kb-<id>/）。

    与 `document_key` 保持同构，便于运维按前缀清理；`kb-` 前缀让两类文档在同一个
    bucket 里也能一眼区分（UUID 前缀不会撞上应用目录）。
    """
    ext = ext.lower().lstrip(".")
    if not _EXT_RE.match(ext):
        raise ValueError(f"非法扩展名: {ext!r}")
    return f"kb-{UUID(str(kb_id))}/{UUID(str(document_id))}/v{version}.{ext}"


def kb_prefix(kb_id: UUID) -> str:
    return f"kb-{UUID(str(kb_id))}/"

def app_prefix(app_id: UUID) -> str:
    return f"{UUID(str(app_id))}/"


@runtime_checkable
class Storage(Protocol):
    async def put(self, key: str, data: bytes) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def exists(self, key: str) -> bool: ...

    async def delete(self, key: str) -> bool: ...

    async def delete_prefix(self, prefix: str) -> int: ...
