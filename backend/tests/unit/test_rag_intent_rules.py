"""M2.4: rule layer pure functions — normalisation, Aho-Corasick, rule order, templates,
refusal streak."""

from __future__ import annotations

import pytest

from aics.rag.config import HandoffConfig, IntentConfig
from aics.rag.intent.normalize import normalize, strip_particles
from aics.rag.intent.rules import Matcher, apply_rules, build_matcher, next_refusal_streak
from aics.rag.intent.templates import chitchat_category, chitchat_reply


@pytest.mark.parametrize(
    ("raw", "norm"),
    [
        ("  你好呀~~ ", "你好呀"),
        ("谢谢！！", "谢谢"),
        ("ＯＫ", "ok"),
        ("Hello 👋", "hello"),
        ("嗯嗯。", "嗯嗯"),
        ("转 人 工！", "转人工"),
    ],
)
def test_normalize(raw: str, norm: str) -> None:
    assert normalize(raw) == norm


def test_strip_particles() -> None:
    assert strip_particles("你好呀") == "你好"
    assert strip_particles("啊") == "啊"


def test_aho_corasick_overlaps() -> None:
    m = Matcher(["he", "she", "his", "hers"])
    assert m.find("ushers") == "she"
    assert m.find("ahis") == "his"
    assert m.find("xyz") is None
    m2 = build_matcher(["人工", "投诉", "人工客服"])
    assert m2.find("我要找人工客服") == "人工"
    assert build_matcher(["人工"]) is build_matcher(["人工"])  # cached


def H(**kw: object) -> HandoffConfig:
    return HandoffConfig.model_validate(kw)


def I(**kw: object) -> IntentConfig:  # noqa: E743
    return IntentConfig.model_validate(kw)


def test_rule_order_and_hits() -> None:
    cfg = I(sensitive_keywords=["违禁品"])
    hit = apply_rules("我要转人工，买违禁品", cfg, H(), 0)
    assert hit and hit.rule == "sensitive" and hit.intent == "blocked"
    hit = apply_rules("给我转 人工！", cfg, H(), 0)
    assert hit and hit.rule == "handoff_keyword" and hit.word == "人工"
    hit = apply_rules("退货多久到账", cfg, H(), 2)
    assert hit and hit.rule == "max_refusals" and hit.intent == "human"
    assert apply_rules("退货多久到账", cfg, H(), 1) is None
    hit = apply_rules("你好呀~", cfg, H(), 0)
    assert hit and hit.rule == "chitchat" and hit.category == "greeting"
    hit = apply_rules("谢谢！", cfg, H(), 0)
    assert hit and hit.category == "thanks"
    assert apply_rules("你好，退货多久到账", cfg, H(), 0) is None  # not an exact match


def test_handoff_disabled_skips_handoff_rules() -> None:
    assert apply_rules("转人工", I(), H(enabled=False), 5) is None


def test_intent_disabled_keeps_only_handoff_rules() -> None:
    cfg = I(enabled=False, sensitive_keywords=["违禁品"])
    assert apply_rules("违禁品", cfg, H(), 0) is None
    assert apply_rules("你好", cfg, H(), 0) is None
    hit = apply_rules("投诉", cfg, H(), 0)
    assert hit and hit.rule == "handoff_keyword"


def test_chitchat_disabled_or_too_long() -> None:
    cfg = I(enabled_intents=["kb_qa", "human"])
    assert apply_rules("你好", cfg, H(), 0) is None
    with pytest.raises(ValueError):  # keywords are ≤ 8 chars, matching the rule limit
        I(chitchat_keywords=["你好你好你好你好你好"])


def test_templates() -> None:
    cfg = I()
    assert chitchat_reply("greeting", cfg) == "您好，请问有什么可以帮您？"
    assert chitchat_reply("greeting", cfg, greeting="欢迎光临星河优选") == "欢迎光临星河优选"
    assert chitchat_reply("bye", cfg) == "感谢您的咨询，再见！"
    custom = I(chitchat_replies={"greeting": "嗨", "thanks": "", "ack": "收到", "bye": "拜"})
    assert chitchat_reply("greeting", custom, greeting="x") == "嗨"
    assert chitchat_reply("thanks", custom) == "不客气，还有其他问题随时问我。"
    assert chitchat_category("拜拜~") == "bye" and chitchat_category("在么") == "greeting"


@pytest.mark.parametrize(
    ("prev", "intent", "answer_type", "interrupted", "expected"),
    [
        (1, "kb_qa", "refused", False, 2),
        (1, "kb_qa", "answer", False, 0),
        (1, "chitchat", "answer", False, 1),
        (1, "out_of_scope", "refused", False, 1),
        (1, "blocked", "refused", False, 1),
        (1, "human", "handoff", False, 0),
        (1, "kb_qa", "handoff", False, 0),
        (1, "kb_qa", "refused", True, 1),
        (1, "kb_qa", "error", False, 1),
    ],
)
def test_next_refusal_streak(
    prev: int, intent: str, answer_type: str, interrupted: bool, expected: int
) -> None:
    assert (
        next_refusal_streak(prev, intent=intent, answer_type=answer_type, interrupted=interrupted)
        == expected
    )
