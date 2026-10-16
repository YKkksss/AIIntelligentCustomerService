from typing import Any, cast

import pytest
import yaml
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from aics.core.errors import ValidationAppError
from aics.core.models import ModelRegistry
from aics.ingest.chunker import ChunkOptions
from aics.modules.apps.service import AppService, _merge_chunk, list_chunk_presets
from aics.rag.config import (
    PLATFORM_DEFAULT_SESSION,
    PRESETS_FILE,
    AppRagConfig,
    ChunkConfig,
    ConfigValidationError,
    HandoffConfig,
    chunk_presets,
    default_chunk_preset,
    validate_app_config,
)

EXPECTED = {
    "general": ("heading", 500, 80),
    "legal": ("article", 400, 50),
    "paper": ("section", 800, 120),
    "faq": ("faq", 300, 0),
    "manual": ("heading", 600, 100),
    "contract": ("clause", 500, 60),
}


def test_presets_come_from_yaml_and_are_valid() -> None:
    raw = yaml.safe_load(PRESETS_FILE.read_text(encoding="utf-8"))
    assert default_chunk_preset() == raw["default"] == "general"
    assert [p.name for p in chunk_presets()] == list(raw["presets"])
    for p in chunk_presets():
        cfg = ChunkConfig.model_validate({"preset": p.name})
        assert (cfg.strategy, cfg.chunk_size, cfg.chunk_overlap) == EXPECTED[p.name]
        assert p.label and p.description
        ChunkOptions.from_mapping(cfg.model_dump()).validate()  # 每个预设都能直接驱动切块器


def test_list_presets_api_shape() -> None:
    items = list_chunk_presets()
    assert [i["name"] for i in items] == list(EXPECTED)
    assert items[1]["config"]["preset"] == "legal" and items[1]["config"]["strategy"] == "article"
    assert set(items[0]["config"]) >= {"chunk_size", "separators", "mode", "parent_size"}


def test_preset_fill_and_override() -> None:
    c = ChunkConfig.model_validate({"preset": "manual", "chunk_overlap": 40})
    assert (c.chunk_size, c.chunk_overlap, c.preset) == (600, 40, "manual")
    custom = ChunkConfig.model_validate(
        {"preset": "custom", "chunk_size": 300, "chunk_overlap": 10}
    )
    assert custom.strategy == "heading" and custom.preset == "custom"
    with pytest.raises(ValidationError, match="未知切块预设"):
        ChunkConfig.model_validate({"preset": "poetry"})


@pytest.mark.parametrize(
    "raw",
    [
        {"separators": [""]},
        {"separators": ["x" * 17]},
        {"separators": ["   "]},
        {"separators": ["a\x00"]},
        {"separators": ["#"] * 11},
        {"strategy": "regex"},
        {"mode": "parent_child", "parent_size": 400, "child_size": 400},
        {"mode": "parent_child", "child_size": 100, "child_overlap": 60},
        {"parent_size": 100},
    ],
)
def test_invalid_chunk_config(raw: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ChunkConfig.model_validate(raw)


def test_separators_dedup_and_blank_line_allowed() -> None:
    assert ChunkConfig(separators=["###", "\n\n", "###"]).separators == ["###", "\n\n"]


def test_window_uses_parent_size_in_parent_child_mode() -> None:
    reg = ModelRegistry.model_validate(
        [{"name": "m", "kind": "chat", "context_window": 16_000, "price_in": 0, "price_out": 0}]
    )
    args: dict[str, Any] = {
        "rag": AppRagConfig(llm_model="m"),
        "handoff": HandoffConfig(),
        "session": PLATFORM_DEFAULT_SESSION,
        "models": reg,
        "default_llm_model": "m",
        "message_embed_enabled": False,
    }
    # 通用：3000 + 800 + 5×500 + 1500 = 7800 ≤ 12800
    validate_app_config(chunk=ChunkConfig(), **args)
    # 父子：3000 + 800 + 5×2000 + 1500 = 15300 > 12800（子块再小也按父块计）
    pc = ChunkConfig(mode="parent_child", parent_size=2000, child_size=100)
    with pytest.raises(ConfigValidationError, match="parent_size"):
        validate_app_config(chunk=pc, **args)


def test_service_merge_chunk_preset() -> None:
    cur = ChunkConfig(preset="manual", chunk_overlap=40).model_dump()
    assert _merge_chunk(cur, {"chunk_size": 700})["chunk_overlap"] == 40
    assert _merge_chunk(cur, {"preset": "faq"}) == {"preset": "faq"}
    svc = AppService(cast(AsyncSession, object()))
    cfg = svc.build_config(
        rag_config=None,
        handoff_config=None,
        session_config=None,
        chunk_config={"preset": "legal", "chunk_size": 450},
    )
    assert (cfg.chunk.strategy, cfg.chunk.chunk_size, cfg.chunk.chunk_overlap) == (
        "article",
        450,
        50,
    )
    with pytest.raises(ValidationAppError):
        svc.build_config(
            rag_config=None,
            handoff_config=None,
            session_config=None,
            chunk_config={"preset": "nope"},
        )
