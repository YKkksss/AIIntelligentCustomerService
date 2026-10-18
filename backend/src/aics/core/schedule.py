"""服务时间判定（M3.4 ②，会话生命周期与转人工 §3.7）。

放在 `core` 而不是 `rag.config` / `modules.handoff`：`chat` 与 `handoff` 都要用它，落在任一侧
都会让两者互相依赖（import-linter 契约 `chat 只可依赖 apps、webhooks` / `handoff 只可依赖
chat、webhooks、apps`）。`core` 谁都不许依赖，因此这里用结构化协议 `WeeklyHours` 描述入参，
`rag.config.ServiceHours`（`timezone` + `weekly`）天然满足它，不必反向 import。

语义：
- 未配置（`None`）= 7×24 都在服务时间；
- 区间**左闭右开** `[start, end)`，不跨零点（`rag.config.ServiceHours` 已校验，这里只做兜底）；
- 按 `cfg.timezone` 的**本地钟面**判定，夏令时跳变当天以该天实际出现的钟点为准。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, time
from typing import Protocol
from zoneinfo import ZoneInfo

#: 与 `rag.config.WEEKDAYS` 一致；`datetime.weekday()` 里周一 = 0。
WEEKDAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class WeeklyHours(Protocol):
    """`rag.config.ServiceHours` 的结构化视图（避免 core → rag 的依赖）。"""

    timezone: str
    weekly: Mapping[str, Sequence[Sequence[str]]]


def _parse(value: str) -> time | None:
    """`HH:MM` → `time`；畸形值返回 None（配置侧已校验，这里只兜底不抛）。"""
    hour, _, minute = value.partition(":")
    if not (hour.isdigit() and minute.isdigit()):
        return None
    h, m = int(hour), int(minute)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return time(h, m)


def in_service_hours(cfg: WeeklyHours | None, now: datetime) -> bool:
    """`now`（UTC）是否落在服务时间内？未配置即 7×24（§3.7）。"""
    if cfg is None:
        return True
    if now.tzinfo is None:  # 兜底：调用方一律传 aware UTC
        now = now.replace(tzinfo=UTC)
    local = now.astimezone(ZoneInfo(cfg.timezone))
    current = local.time()
    for window in cfg.weekly.get(WEEKDAYS[local.weekday()]) or ():
        if len(window) != 2:
            continue
        start, end = _parse(window[0]), _parse(window[1])
        if start is None or end is None or start >= end:
            continue
        if start <= current < end:
            return True
    return False
