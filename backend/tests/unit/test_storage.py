import uuid
from pathlib import Path

import pytest

from aics.storage import (
    InvalidKeyError,
    LocalStorage,
    ObjectNotFoundError,
    Storage,
    app_prefix,
    build_storage,
    document_key,
)

APP, DOC = uuid.uuid4(), uuid.uuid4()


def test_document_key() -> None:
    assert document_key(APP, DOC, 2, ".PDF") == f"{APP}/{DOC}/v2.pdf"
    for bad in ["../x", "p/df", "", "toolongextension"]:
        with pytest.raises(InvalidKeyError):
            document_key(APP, DOC, 1, bad)
    with pytest.raises(InvalidKeyError):
        document_key(APP, DOC, 0, "pdf")


async def test_roundtrip(tmp_path: Path) -> None:
    st = LocalStorage(tmp_path)
    assert isinstance(st, Storage)
    key = document_key(APP, DOC, 1, "md")
    assert not await st.exists(key)
    await st.put(key, b"# hello")
    assert await st.exists(key)
    assert await st.get(key) == b"# hello"
    await st.put(key, b"v2")  # 覆盖
    assert await st.get(key) == b"v2"
    assert not list((tmp_path / str(APP) / str(DOC)).glob(".tmp-*"))
    assert await st.delete(key) is True
    assert await st.delete(key) is False
    with pytest.raises(ObjectNotFoundError):
        await st.get(key)


@pytest.mark.parametrize(
    "key",
    ["../etc/passwd", "/etc/passwd", "a/../../x", "a/./b", "a//b", "a\\b", "", "a\x00b", "."],
)
async def test_path_traversal_rejected(tmp_path: Path, key: str) -> None:
    st = LocalStorage(tmp_path / "root")
    with pytest.raises(InvalidKeyError):
        await st.put(key, b"x")


async def test_symlink_escape_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    st = LocalStorage(root)
    (tmp_path / "outside").mkdir()
    (root / "link").symlink_to(tmp_path / "outside")
    with pytest.raises(InvalidKeyError):
        await st.put("link/x.txt", b"x")


async def test_delete_prefix(tmp_path: Path) -> None:
    st = LocalStorage(tmp_path)
    other = uuid.uuid4()
    await st.put(document_key(APP, DOC, 1, "pdf"), b"1")
    await st.put(document_key(APP, uuid.uuid4(), 1, "pdf"), b"2")
    await st.put(document_key(other, DOC, 1, "pdf"), b"3")
    assert await st.delete_prefix(app_prefix(APP)) == 2
    assert await st.delete_prefix(app_prefix(APP)) == 0
    assert await st.exists(document_key(other, DOC, 1, "pdf"))


def test_factory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from aics.core.config import Settings

    monkeypatch.setenv("AICS_STORAGE_LOCAL_ROOT", str(tmp_path / "s"))
    assert isinstance(build_storage(Settings(_env_file=None)), LocalStorage)  # type: ignore[call-arg]
    monkeypatch.setenv("AICS_STORAGE_BACKEND", "s3")
    with pytest.raises(NotImplementedError):
        build_storage(Settings(_env_file=None))  # type: ignore[call-arg]
