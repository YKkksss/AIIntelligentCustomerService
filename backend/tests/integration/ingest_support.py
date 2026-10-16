"""入库集成测试共用：确定性假嵌入器与样例（fixture 在 conftest.py）。"""

from __future__ import annotations

import hashlib
import os

DB = os.environ.get("AICS_TEST_DB_URL")
QURL = os.environ.get("AICS_TEST_QDRANT_URL")
QKEY = os.environ.get("AICS_TEST_QDRANT_API_KEY")
DIM = 8
MD = ("# 售后\n\n## 退货\n\n" + "签收后七天内可无理由退货，运费规则见下。\n\n" * 40).encode()


class FakeEmbedder:
    model, dim = "fake", DIM

    def __init__(self, fail_on_call: int | None = None) -> None:
        self.calls = 0
        self.fail_on_call = fail_on_call
        self.texts: list[str] = []

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.fail_on_call == self.calls:
            raise RuntimeError("embedding upstream down")
        self.texts += texts
        out = []
        for t in texts:
            h = hashlib.sha256(t.encode()).digest()
            out.append([b / 255 + 0.01 for b in h[:DIM]])
        return out

    async def embed_query(self, text: str) -> list[float]:
        return (await self.embed_documents([text]))[0]
