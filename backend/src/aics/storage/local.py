"""本地文件存储（`AICS_STORAGE_LOCAL_ROOT`）。写入先落临时文件再原子改名，避免读到半个文件。"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath

from aics.storage.base import InvalidKeyError, ObjectNotFoundError


class LocalStorage:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        if not key or "\\" in key or "\x00" in key:
            raise InvalidKeyError(f"非法 key：{key!r}")
        pp = PurePosixPath(key)
        if pp.is_absolute() or any(part in ("", ".", "..") for part in key.split("/")):
            raise InvalidKeyError(f"非法 key：{key!r}")
        path = (self.root / pp).resolve()
        if not path.is_relative_to(self.root) or path == self.root:
            raise InvalidKeyError(f"key 越出存储根目录：{key!r}")
        return path

    def _put_sync(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    async def put(self, key: str, data: bytes) -> None:
        await asyncio.to_thread(self._put_sync, self._path(key), data)

    async def get(self, key: str) -> bytes:
        path = self._path(key)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError as e:
            raise ObjectNotFoundError(key) from e

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self._path(key).is_file)

    async def delete(self, key: str) -> bool:
        path = self._path(key)

        def _rm() -> bool:
            try:
                path.unlink()
                return True
            except FileNotFoundError:
                return False

        return await asyncio.to_thread(_rm)

    async def delete_prefix(self, prefix: str) -> int:
        """删除一个目录前缀（如整个应用 `{app_id}/`）；返回删除的文件数。"""
        path = self._path(prefix.rstrip("/"))

        def _rm() -> int:
            if not path.exists():
                return 0
            if path.is_file():
                path.unlink()
                return 1
            n = sum(1 for p in path.rglob("*") if p.is_file())
            shutil.rmtree(path)
            return n

        return await asyncio.to_thread(_rm)
