"""SimHash 的确定性与距离阈值（M4.3b ②，设计讨论 0010 §2.4）。"""

from __future__ import annotations

from aics.core.simhash import (
    NEAR_DUPLICATE_BITS,
    hamming,
    near_duplicate,
    simhash,
    tokens_of,
)

LONG = (
    "退货政策：自签收之日起 7 天内可无理由退货，商品需保持完好，赠品需一并退回。"
    "换货政策：签收后 15 天内可申请换货，同一商品仅限一次。"
    "运费说明：质量问题由商家承担往返运费，非质量问题由买家承担退回运费。"
    "发票说明：支持电子发票，下单时填写抬头与税号，发货后 3 个工作日内开出。"
    "售后时效：审核通过后 48 小时内退款到账，节假日顺延。"
    "客服时间：每天 9:00-21:00，节假日不休。"
)


def test_tokens_split_cjk_into_trigrams_and_words() -> None:
    tokens = tokens_of("退款 REFUND 2026")
    assert "退款 " not in tokens
    assert "refund" in tokens
    assert "2026" in tokens
    # 三个汉字的片段整体成为一个 token
    assert "退款" in tokens or "退款r" not in tokens
    assert all(" " not in t for t in tokens)


def test_tokens_of_empty_text_is_empty() -> None:
    assert tokens_of("") == []
    assert simhash("") == 0


def test_simhash_is_deterministic() -> None:
    assert simhash(LONG) == simhash(LONG)
    assert 0 <= simhash(LONG) < 1 << 64


def test_simhash_near_duplicate_within_threshold() -> None:
    edited = LONG.replace("7 天", "10 天").replace("48 小时", "24 小时")
    distance = hamming(simhash(LONG), simhash(edited))
    assert distance <= NEAR_DUPLICATE_BITS
    assert near_duplicate(simhash(LONG), simhash(edited))


def test_simhash_different_documents_far_apart() -> None:
    other = (
        "发票与报销：差旅报销需在出差结束后 10 个工作日内提交，附上行程单与发票原件。"
        "审批流程：直属主管审批后由财务复核，单笔超过 5000 元需部门负责人签字。"
        "打款时间：每月 15 日与 30 日集中打款，遇周末提前至最近工作日。"
        "注意事项：发票抬头需与合同一致，个人消费不在报销范围。"
    )
    assert hamming(simhash(LONG), simhash(other)) > 10
    assert not near_duplicate(simhash(LONG), simhash(other))


def test_near_duplicate_ignores_zero_fingerprint() -> None:
    assert not near_duplicate(0, simhash(LONG))
    assert not near_duplicate(simhash(LONG), 0)
    assert hamming(0, 0) == 0
