"""智能客服头像的校验与默认值（`modules/apps/avatar.py`）。"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from aics.core.errors import ValidationAppError
from aics.modules.apps.avatar import (
    AVATAR_STYLES,
    DEFAULT_AVATAR_STYLE,
    avatar_of,
    normalize_avatar,
)


def _locs(exc: ValidationAppError) -> list[list[str]]:
    return [list(e["loc"]) for e in exc.details["errors"]]


def test_none_and_empty_mean_default() -> None:
    assert normalize_avatar(None) == {}
    assert normalize_avatar({}) == {}


def test_minimal_payload_fills_style_and_keeps_seed() -> None:
    body = normalize_avatar({"seed": "abc"})
    assert body["style"] == DEFAULT_AVATAR_STYLE
    assert body["seed"] == "abc"
    assert body["url"] is None


def test_url_must_be_http_and_is_optional() -> None:
    assert normalize_avatar({"seed": "a", "url": "https://cdn.example.com/a.png"})["url"] == (
        "https://cdn.example.com/a.png"
    )
    assert normalize_avatar({"seed": "a", "url": ""})["url"] is None
    with pytest.raises(ValidationAppError) as e:
        normalize_avatar({"url": "ftp://example.com/a.png"})
    assert _locs(e.value) == [["avatar", "url"]]


def test_unknown_style_is_rejected_with_field_path() -> None:
    with pytest.raises(ValidationAppError) as e:
        normalize_avatar({"style": "shapes"})
    assert _locs(e.value) == [["avatar", "style"]]


def test_unknown_key_and_bad_types() -> None:
    with pytest.raises(ValidationAppError) as e:
        normalize_avatar({"seed": "a", "expression": "smile"})
    assert _locs(e.value) == [["avatar", "expression"]]
    with pytest.raises(ValidationAppError) as e2:
        normalize_avatar({"seed": 7})
    assert _locs(e2.value) == [["avatar", "seed"]]
    with pytest.raises(ValidationAppError):
        normalize_avatar("notionists")  # type: ignore[arg-type]


def test_seed_length_is_bounded() -> None:
    with pytest.raises(ValidationAppError) as e:
        normalize_avatar({"seed": "x" * 65})
    assert _locs(e.value) == [["avatar", "seed"]]


def test_every_allowed_style_round_trips() -> None:
    for style in AVATAR_STYLES:
        assert normalize_avatar({"style": style, "seed": "s"})["style"] == style


def test_avatar_of_fills_stable_default_from_app_id() -> None:
    app = SimpleNamespace(id=uuid.uuid4(), avatar={})
    first = avatar_of(app)
    assert first == {"style": DEFAULT_AVATAR_STYLE, "seed": str(app.id), "url": None}
    assert avatar_of(app) == first  # 同一应用永远同一张脸


def test_avatar_of_prefers_stored_values() -> None:
    app = SimpleNamespace(id=uuid.uuid4(), avatar={"style": "pixelArt", "seed": "s1", "url": None})
    assert avatar_of(app) == {"style": "pixelArt", "seed": "s1", "url": None}


def test_avatar_of_ignores_corrupt_style() -> None:
    app = SimpleNamespace(id=uuid.uuid4(), avatar={"style": "gone", "seed": "s2"})
    assert avatar_of(app)["style"] == DEFAULT_AVATAR_STYLE
