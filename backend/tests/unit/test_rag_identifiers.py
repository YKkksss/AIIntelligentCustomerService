"""M2.5 ④: identifier consistency guardrail (RAG 流程设计 §4 postprocess)."""

from __future__ import annotations

import pytest

from aics.core import metrics
from aics.rag.graph import run_rag_collect
from aics.rag.identifiers import check_identifiers, find_identifiers
from aics.rag.ports import ContextBundle, ContextRequest
from tests.rag_support import FakeLLMProvider
from tests.unit.test_rag_graph import inp, kb_with, make_responder, ports


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("您的订单 A20261014001 已签收", [("order", "A20261014001")]),
        ("联系电话 13812345678。", [("phone", "13812345678")]),
        ("订单号XH-WL-2026-0158", [("order", "XH-WL-2026-0158")]),
        ("2026-10-14 前发货，20261014 截止", []),  # dates
        ("型号 F5-HEPA13", []),  # < 6 digits
        ("价格 1299 元，退款 7 天", []),
        ("ABC1234567890123456789012345678901234567", []),  # > 32 chars
    ],
)
def test_find_identifiers(text: str, expected: list[tuple[str, str]]) -> None:
    assert find_identifiers(text) == expected


def test_check_sources_of_truth() -> None:
    facts = {"order_no": "A20261014001", "phone": "13812345678", "product": "B20261015002"}
    ok = check_identifiers(
        "订单 A2026-1014-001 与电话 138 1234 5678 已登记", facts=facts, question="", sources=[]
    )
    # Hyphen / spacing variants of the fact value are the same identifier.
    assert ok.checked == 1 and not ok.mismatch  # spaced phone is split into tokens (not 8+)
    bad = check_identifiers("您的订单 B20261015002 已退款", facts=facts, question="", sources=[])
    assert bad.mismatched == [("order", "B20261015002")]  # product key does not count
    q = check_identifiers(
        "订单 C20261016003 已退款", facts={}, question="C20261016003 呢", sources=[]
    )
    assert not q.mismatch
    s = check_identifiers(
        "请拨打 4008123123 或 13900001111",
        facts={},
        question="",
        sources=["客服热线 4008123123", "电话 13900001111"],
    )
    assert not s.mismatch and s.checked == 2
    assert check_identifiers("没有标识符", facts={}, question="", sources=[]).checked == 0


async def test_postprocess_flags_mismatch_and_counts() -> None:
    metrics.reset()

    class Mem:
        async def load_context(self, req: ContextRequest) -> ContextBundle:
            return ContextBundle(facts={"order_no": "A20261014001"})

    q = "退款几天到账"
    llm = FakeLLMProvider(make_responder(generate="您的订单 B99990000123 退款已到账[1]"))
    res, _ = await run_rag_collect(inp(q), ports(kb_with({q: 0.95}), llm, memory=Mem()))
    assert res.answer_type == "answer"
    assert "identifier_mismatch" in res.degraded
    assert res.trace["postprocess"]["identifiers"] == {
        "checked": 1,
        "mismatched": ["********0123"],
    }
    assert res.answer.startswith("您的订单 B99990000123")  # never rewritten
    assert metrics.value("aics_identifier_mismatch_total") == 1

    # The same answer is fine when the number is the user's current fact.
    class Mem2:
        async def load_context(self, req: ContextRequest) -> ContextBundle:
            return ContextBundle(facts={"order_no": "B99990000123"})

    res2, _ = await run_rag_collect(inp(q), ports(kb_with({q: 0.95}), llm, memory=Mem2()))
    assert "identifier_mismatch" not in res2.degraded
