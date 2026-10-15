"""文件存储（L1，只依赖 core）。"""

from __future__ import annotations

from functools import lru_cache

from aics.core.config import Settings, get_settings
from aics.storage.base import (
    InvalidKeyError,
    ObjectNotFoundError,
    Storage,
    StorageError,
    app_prefix,
    document_key,
)
from aics.storage.local import LocalStorage

__all__ = [
    "InvalidKeyError",
    "LocalStorage",
    "ObjectNotFoundError",
    "Storage",
    "StorageError",
    "app_prefix",
    "build_storage",
    "document_key",
    "get_storage",
]


def build_storage(s: Settings) -> Storage:
    if s.storage_backend == "local":
        return LocalStorage(s.storage_local_root)
    raise NotImplementedError("s3 存储计划在 v2 实现")


@lru_cache(maxsize=1)
def get_storage() -> Storage:
    return build_storage(get_settings())
