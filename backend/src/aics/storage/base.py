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


def app_prefix(app_id: UUID) -> str:
    return f"{UUID(str(app_id))}/"


@runtime_checkable
class Storage(Protocol):
    async def put(self, key: str, data: bytes) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def exists(self, key: str) -> bool: ...

    async def delete(self, key: str) -> bool: ...

    async def delete_prefix(self, prefix: str) -> int: ...
