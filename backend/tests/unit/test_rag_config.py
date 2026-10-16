import pytest
from pydantic import ValidationError

from aics.core.models import ModelRegistry
from aics.rag.config import (
    INTENTS,
    PLATFORM_DEFAULT_SESSION,
    AppRagConfig,
    ChunkConfig,
    ConfigValidationError,
    EffectiveSessionConfig,
    HandoffConfig,
    IntentConfig,
    ServiceHours,
    SessionConfig,
    WebhookConfig,
    validate_app_config,
)

REG = ModelRegistry.model_validate(
    [
        {"name": "big", "kind": "chat", "context_window": 128_000, "price_in": 0, "price_out": 0},
        {"name": "small", "kind": "chat", "context_window": 8_000, "price_in": 0, "price_out": 0},
        {"name": "emb", "kind": "embedding", "context_window": 8192, "price_in": 0, "price_out": 0},
    ]
)


def _validate(**kw: object) -> None:
    args: dict[str, object] = {
        "rag": AppRagConfig(),
        "chunk": ChunkConfig(),
        "handoff": HandoffConfig(),
        "session": PLATFORM_DEFAULT_SESSION,
        "models": REG,
        "default_llm_model": "big",
        "message_embed_enabled": False,
    }
    args.update(kw)
    validate_app_config(**args)  # type: ignore[arg-type]


def test_defaults_match_design() -> None:
    rag = AppRagConfig.model_validate({})
    d = rag.model_dump()
    assert d["vector_top_k"] == 20 and d["fuse_top_k"] == 30 and d["rerank_top_n"] == 5
    assert d["rerank_threshold"] == 0.35 and d["judge_gray_upper"] == 0.5
    assert d["history_token_budget"] == 3000 and d["compact_trigger_tokens"] == 6000
    assert d["llm_model"] is None and d["max_tokens"] == 800
    assert d["intent"]["enabled_intents"] == list(INTENTS)
    assert d["intent"]["vector_threshold"] == 0.9
    h = HandoffConfig().model_dump()
    assert h["keywords"] == ["人工", "投诉"] and h["handoff_queue_timeout_minutes"] == 5
    assert h["handoff_accepted_idle_minutes"] == 30 and h["service_hours"] is None
    c = ChunkConfig().model_dump()
    assert (c["preset"], c["strategy"], c["chunk_size"], c["chunk_overlap"]) == (
        "general",
        "heading",
        500,
        80,
    )
    assert c["mode"] == "general" and c["separators"] == [] and c["split_by_heading"] is True
    assert SessionConfig().model_dump() == dict.fromkeys(
        ["idle_close_minutes", "idle_notice_before_minutes", "idle_close_notice"]
    )
    assert WebhookConfig().public() == {"url": None, "secret_prefix": None}


def test_partial_config_filled_and_legacy_keys_ignored() -> None:
    rag = AppRagConfig.model_validate(
        {"vector_top_k": 10, "history_turns": 6, "intent": {"min_confidence": 0.5}}
    )
    assert rag.vector_top_k == 10 and rag.keyword_top_k == 20
    assert rag.intent.min_confidence == 0.5 and rag.intent.clarify_threshold == 0.7
    assert "history_turns" not in rag.model_dump()


@pytest.mark.parametrize(
    "raw",
    [
        {"compact_trigger_tokens": 2000},  # < history_token_budget
        {"history_token_budget": 100},
        {"rerank_threshold": 0.6, "judge_gray_upper": 0.5},
        {"intent": {"enabled_intents": ["chitchat"]}},
        {"intent": {"enabled_intents": ["kb_qa", "bogus"]}},
        {"intent": {"min_confidence": 0.8, "clarify_threshold": 0.7}},
        {"intent": {"chitchat_keywords": ["这是一个超过八个字的寒暄"]}},
        {"intent": {"vector_threshold": 0.5}},
        {"keep_recent_turns": 0},
    ],
)
def test_invalid_rag_config(raw: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AppRagConfig.model_validate(raw)


def test_intent_dedup() -> None:
    assert IntentConfig(enabled_intents=["kb_qa", "kb_qa"]).enabled_intents == ["kb_qa"]


def test_chunk_config_rules() -> None:
    with pytest.raises(ValidationError):
        ChunkConfig(chunk_size=200, chunk_overlap=150)
    with pytest.raises(ValidationError):
        ChunkConfig(chunk_size=50)


def test_session_config_and_resolve() -> None:
    assert SessionConfig().resolve(PLATFORM_DEFAULT_SESSION) == PLATFORM_DEFAULT_SESSION
    eff = SessionConfig(idle_close_minutes=30).resolve(PLATFORM_DEFAULT_SESSION)
    assert eff.idle_close_minutes == 30 and eff.idle_notice_before_minutes == 1
    with pytest.raises(ValidationError):
        SessionConfig(idle_close_minutes=5, idle_notice_before_minutes=5)
    with pytest.raises(ValidationError):  # close=1 时 notice 只能为 0（合并平台默认 1 后失败）
        SessionConfig(idle_close_minutes=1).resolve(PLATFORM_DEFAULT_SESSION)
    assert SessionConfig(idle_close_minutes=1, idle_notice_before_minutes=0).resolve(
        PLATFORM_DEFAULT_SESSION
    )
    with pytest.raises(ValidationError):
        SessionConfig(idle_close_minutes=2000)
    with pytest.raises(ValidationError):
        SessionConfig(idle_close_notice="")


def test_service_hours() -> None:
    ok = ServiceHours.model_validate(
        {
            "timezone": "Asia/Shanghai",
            "weekly": {"wed": [["09:00", "12:00"], ["13:30", "18:00"]], "sat": []},
        }
    )
    assert ok.weekly["wed"][1] == ("13:30", "18:00")
    for bad in [
        {"timezone": "Mars/Base", "weekly": {}},
        {"timezone": "UTC", "weekly": {"xyz": []}},
        {"timezone": "UTC", "weekly": {"mon": [["18:00", "09:00"]]}},
        {"timezone": "UTC", "weekly": {"mon": [["9:00", "18:00"]]}},
        {"timezone": "UTC", "weekly": {"mon": [["09:00", "12:00"], ["11:00", "13:00"]]}},
    ]:
        with pytest.raises(ValidationError):
            ServiceHours.model_validate(bad)


def test_handoff_rules() -> None:
    with pytest.raises(ValidationError):
        HandoffConfig(handoff_ai_mode="pause")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        HandoffConfig(handoff_queue_timeout_minutes=0)
    with pytest.raises(ValidationError):
        HandoffConfig(wait_text="")
    with pytest.raises(ValidationError):
        HandoffConfig(accepted_text="x" * 201)


def test_webhook_url_format() -> None:
    assert WebhookConfig(url="https://cs.example.com/hook").url
    with pytest.raises(ValidationError):
        WebhookConfig(url="ftp://x")


def test_cross_validation_ok() -> None:
    _validate()


def test_window_80_percent() -> None:
    # 3000 + 800 + 5×500 + 1500 = 7800 > 0.8 × 8000 = 6400
    with pytest.raises(ConfigValidationError) as e:
        _validate(rag=AppRagConfig(llm_model="small"))
    assert any("80%" in i.msg for i in e.value.issues)
    # 压缩模型 8000 + 1000 > 6400
    assert any(i.loc == ("rag_config", "summary_model") for i in e.value.issues)
    # 边界：0.8 × 12000 = 9600
    reg = ModelRegistry.model_validate(
        [{"name": "m", "kind": "chat", "context_window": 12_000, "price_in": 0, "price_out": 0}]
    )
    rag = AppRagConfig(
        llm_model="m", history_token_budget=4000, max_tokens=1000, rerank_top_n=6
    )  # 4000 + 1000 + 3000 + 1500 = 9500 ≤ 9600
    _validate(rag=rag, models=reg)
    with pytest.raises(ConfigValidationError):
        _validate(rag=rag.model_copy(update={"max_tokens": 1101}), models=reg)


def test_default_llm_model_used_for_window() -> None:
    with pytest.raises(ConfigValidationError):
        _validate(default_llm_model="small")


def test_models_must_be_registered_chat() -> None:
    with pytest.raises(ConfigValidationError) as e:
        _validate(rag=AppRagConfig(llm_model="emb", summary_model="nope"))
    locs = {i.loc for i in e.value.issues}
    assert ("rag_config", "llm_model") in locs and ("rag_config", "summary_model") in locs
    assert "llm_model" in str(e.value)


def test_history_recall_requires_message_embed() -> None:
    with pytest.raises(ConfigValidationError):
        _validate(rag=AppRagConfig(enable_history_recall=True))
    _validate(rag=AppRagConfig(enable_history_recall=True), message_embed_enabled=True)


def test_queue_timeout_below_idle_close() -> None:
    session = EffectiveSessionConfig(
        idle_close_minutes=5, idle_notice_before_minutes=1, idle_close_notice="x"
    )
    with pytest.raises(ConfigValidationError):
        _validate(session=session)
    _validate(session=session, handoff=HandoffConfig(handoff_queue_timeout_minutes=4))
