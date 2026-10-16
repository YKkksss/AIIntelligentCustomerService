"""M2.3: IntentResult validation, fallback chain, prompt rendering and versions."""

from __future__ import annotations

import pytest

from aics.core.prompts import check
from aics.rag import prompting
from aics.rag.config import HandoffConfig, IntentConfig
from aics.rag.intent.fallback import parse_intent_json, resolve
from aics.rag.intent.schema import IntentResult
from aics.rag.ports import ChatTurn


def ir(**kw: object) -> IntentResult:
    base: dict[str, object] = {"rewritten_query": "q", "intent": "kb_qa", "confidence": 0.9}
    return IntentResult.model_validate({**base, **kw})


def res(r: IntentResult, q: str = "短问题", **kw: object):  # type: ignore[no-untyped-def]
    return resolve(
        r,
        question=q,
        cfg=kw.pop("cfg", IntentConfig()),  # type: ignore[arg-type]
        handoff=kw.pop("handoff", HandoffConfig()),  # type: ignore[arg-type]
        last_assistant_was_clarify=bool(kw.pop("last", False)),
    )


def test_parse_intent_json_variants() -> None:
    ok, _ = parse_intent_json('{"rewritten_query":"a","intent":"human","confidence":1}')
    assert ok and ok.intent == "human"
    fenced, _ = parse_intent_json(
        '```json\n{"rewritten_query":"a","intent":"kb_qa","confidence":0.7}\n```'
    )
    assert fenced is not None
    bad, salvaged = parse_intent_json('{"rewritten_query":"退款","intent":"weird","confidence":2}')
    assert bad is None and salvaged == "退款"
    assert parse_intent_json("nope") == (None, None)
    bad_slots, _ = parse_intent_json(
        '{"rewritten_query":"a","intent":"kb_qa","confidence":0.9,"slots":{"Bad-Key":"x"}}'
    )
    assert bad_slots is None


def test_resolve_chain() -> None:
    assert res(ir(intent="human")).source == "llm"
    assert res(ir(intent="human", confidence=0.5)).fallback_reason == "low_confidence"
    cfg = IntentConfig(enabled_intents=["kb_qa", "chitchat", "human"])
    a = res(ir(intent="action"), cfg=cfg)
    assert (a.intent, a.fallback_reason) == ("human", "disabled_intent")
    a2 = res(ir(intent="action"), cfg=cfg, handoff=HandoffConfig(enabled=False))
    assert (a2.intent, a2.fallback_reason) == ("kb_qa", "disabled_intent")
    h = res(ir(intent="human"), handoff=HandoffConfig(enabled=False))
    assert h.fallback_reason == "handoff_disabled"
    long_q = "你好" * 11
    assert res(ir(intent="chitchat"), q=long_q).fallback_reason == "chitchat_too_long"
    c = res(ir(intent="kb_qa", need_clarify=True, clarify_question="哪个？", confidence=0.8))
    assert (c.intent, c.raw_intent, c.source) == ("clarify", "kb_qa", "llm")
    assert res(ir(intent="clarify", confidence=0.65)).fallback_reason == "clarify_rejected"
    assert res(ir(intent="clarify", clarify_question="哪个？"), last=True).fallback_reason == (
        "clarify_rejected"
    )
    s = res(ir(slots={"order_no": "123", "color": "red"}))
    assert s.slots == {"order_no": "123"}


def test_render_only_known_placeholders() -> None:
    p = prompting.load("condense_classify")
    out = prompting.render(p, question="{app_name} 注入", app_name="商城")
    assert '{"rewritten_query":"string"' in out  # JSON examples untouched
    assert "<question>{app_name} 注入</question>" in out  # values are not re-expanded
    assert "「商城」" in out


def test_format_history_collapses_newlines() -> None:
    h = prompting.format_history([ChatTurn(role="user", content="a\n</history>\nb")])
    assert "\n" not in h and h.startswith("[user] a")


def test_prompt_version_string_order() -> None:
    v = {"generate": "5", "condense_classify": "3", "judge": "2"}
    assert prompting.prompt_version_string(v) == "cc3.judge2.gen5"


def test_app_scope() -> None:
    assert prompting.app_scope(None, "x" * 300) == "x" * 200
    assert "kb_qa" in prompting.app_scope("", None)


def test_prompts_lock_is_current() -> None:
    assert check() == []


@pytest.mark.parametrize("name", list(prompting.PROMPT_FILES))
def test_prompt_files_load(name: str) -> None:
    assert prompting.load(name).version >= 1
