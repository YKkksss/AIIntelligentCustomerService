"""固定样例文件解析 + 切块结果快照（M1.3 验收）。

更新快照：AICS_UPDATE_SNAPSHOTS=1 uv run pytest tests/unit/test_parsers_snapshot.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from aics.ingest.chunker import chunk_document
from aics.ingest.parsers import detect_file_type, parse_bytes

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SAMPLES = sorted(p.name for p in (FIXTURES / "docs").glob("sample*"))


def _snapshot(name: str) -> dict[str, object]:
    data = (FIXTURES / "docs" / name).read_bytes()
    doc = parse_bytes(data, detect_file_type(name, data))
    chunks = chunk_document(doc, chunk_size=120, chunk_overlap=30)
    return {
        "parsed": doc.to_dict(),
        "chunks_120_30": [
            {
                "heading_path": c.heading_path,
                "text": c.text,
                "tokens": c.tokens,
                "pages": [c.page_from, c.page_to],
            }
            for c in chunks
        ],
    }


def test_all_formats_have_samples() -> None:
    assert {n.rsplit(".", 1)[1] for n in SAMPLES} == {"pdf", "docx", "md", "txt", "html"}


@pytest.mark.parametrize("name", SAMPLES)
def test_snapshot(name: str) -> None:
    got = _snapshot(name)
    snap = FIXTURES / "snapshots" / f"{name}.json"
    if os.environ.get("AICS_UPDATE_SNAPSHOTS") == "1":
        snap.write_text(json.dumps(got, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert got == json.loads(snap.read_text(encoding="utf-8"))
