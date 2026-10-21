"""智能客服头像（`apps.avatar`，迁移 0017）。

只存「怎么画」的三个字段，**不存图片**：

    {"style": "notionists", "seed": "6f0c…", "url": null}

前端按 `style` + `seed` 用 DiceBear 在浏览器里生成 SVG（同一组参数永远画出同一张脸），
换头像就是换 `seed`，服务端零文件、零带宽。`url` 预留给以后「上传自己的图」，
本迭代不做上传，但允许填写 http(s) 地址，前端优先用图片。

风格只放 MIT 许可的那几个：`CC BY` 的风格（personas / adventurer 等）一旦写进产品，
将来对外时就要考虑署名，不值当。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from aics.core.errors import ValidationAppError

AVATAR_STYLES = ("notionists", "lorelei", "openPeeps", "thumbs", "pixelArt")
DEFAULT_AVATAR_STYLE = "notionists"
SEED_MAX_CHARS = 64
URL_MAX_CHARS = 2048
_AVATAR_KEYS = ("style", "seed", "url")
# 与 `rag.config.WebhookConfig.url` 同一条规则：只认 http(s)，不接受裸域名
_URL_RE = re.compile(r"^https?://\S+$")


def _error(loc: tuple[str, ...], msg: str) -> ValidationAppError:
    return ValidationAppError(details={"errors": [{"loc": list(loc), "msg": msg}]})


def normalize_avatar(value: Mapping[str, Any] | None, *, seed_fallback: str = "") -> dict[str, Any]:
    """校验并补默认值；`None` / 空对象表示「用默认头像」，返回 `{}`。

    非法值抛 422，`details.errors` 的 `loc` 是 `["avatar", "style"]` 这种两段路径，
    前端能直接定位到配置页的那一行。
    """
    if value is None or not value:
        return {}
    if not isinstance(value, Mapping):
        raise _error(("avatar",), "应为对象，形如 {style, seed, url}")
    unknown = sorted(set(value) - set(_AVATAR_KEYS))
    if unknown:
        raise _error(("avatar", unknown[0]), "未知字段")

    style = value.get("style") or DEFAULT_AVATAR_STYLE
    if not isinstance(style, str) or style not in AVATAR_STYLES:
        raise _error(("avatar", "style"), f"应为 {'/'.join(AVATAR_STYLES)} 之一")

    raw_seed = value.get("seed")
    if raw_seed is None or (isinstance(raw_seed, str) and not raw_seed.strip()):
        seed = seed_fallback
    elif not isinstance(raw_seed, str):
        raise _error(("avatar", "seed"), "应为字符串")
    else:
        seed = raw_seed.strip()
        if len(seed) > SEED_MAX_CHARS:
            raise _error(("avatar", "seed"), f"最多 {SEED_MAX_CHARS} 个字符")

    raw_url = value.get("url")
    if raw_url in (None, ""):
        url = None
    elif not isinstance(raw_url, str) or len(raw_url) > URL_MAX_CHARS or not _URL_RE.match(raw_url):
        raise _error(("avatar", "url"), "应为 http(s) 地址，或留空")
    else:
        url = raw_url

    return {"style": style, "seed": seed, "url": url}


def avatar_of(app: Any) -> dict[str, Any]:
    """读出可直接下发的头像；老数据（`{}`）按应用 id 补一个稳定的默认 seed。"""
    stored = getattr(app, "avatar", None) or {}
    style = stored.get("style")
    seed = stored.get("seed")
    return {
        "style": style if style in AVATAR_STYLES else DEFAULT_AVATAR_STYLE,
        "seed": seed or str(app.id),
        "url": stored.get("url"),
    }


__all__ = [
    "AVATAR_STYLES",
    "DEFAULT_AVATAR_STYLE",
    "SEED_MAX_CHARS",
    "avatar_of",
    "normalize_avatar",
]
