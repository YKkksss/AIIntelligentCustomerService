"""M2.6 ② pure parts of chat.service: dispatch table, citation format, profile."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from aics.modules.chat.service import citation_dict, dispatch_of, profile_of
from aics.rag.schemas import Citation


@pytest.mark.parametrize(
    "status,mode,expected",
    [
        ("none", "continue", "rag"),
        ("none", "wait", "rag"),
        ("released", "wait", "rag"),
        ("pending", "continue", "rag"),
        ("pending", "wait", "wait"),
        ("accepted", "continue", "agent"),
        ("accepted", "wait", "agent"),
    ],
)
def test_dispatch_table(status: str, mode: str, expected: str) -> None:
    assert dispatch_of(status, mode) == expected  # 会话生命周期与转人工 §3.2


def test_citation_dict_matches_open_api_object() -> None:
    c = Citation(
        index=2,
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_title="售后政策.pdf",
        heading_path="售后 > 退款",
        page=3,
        snippet="审核通过后……",
        rerank_score=0.823456,
    )
    d = citation_dict(c)
    assert list(d) == [
        "n", "chunk_id", "document_id", "document_title", "heading_path", "snippet", "score", "page"
    ]  # fmt: skip
    assert d["n"] == 2 and d["score"] == 0.8235 and d["page"] == 3
    assert citation_dict(c.model_copy(update={"rerank_score": None}))["score"] is None


def test_profile_of_app_row() -> None:
    app = SimpleNamespace(
        name="商城",
        description="卖家电",
        greeting="您好",
        system_prompt=None,
        answer_style="concise",
        fallback_text="暂时无法回答",
        language="zh-CN",
    )
    p = profile_of(app)
    assert p.name == "商城" and p.fallback_text == "暂时无法回答" and p.greeting == "您好"
