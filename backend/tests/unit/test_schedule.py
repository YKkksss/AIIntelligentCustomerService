"""M3.4 ②：服务时间判定（会话生命周期与转人工 §3.7）。

纯函数，不需要数据库：`weekly` 用 `rag.config.ServiceHours` 的形状（`core.schedule.WeeklyHours`
是结构化协议，核心层不能 import rag）。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aics.core.schedule import in_service_hours


class Hours:
    """`rag.config.ServiceHours` 的最小替身（`timezone` + `weekly`）。"""

    def __init__(self, timezone: str, weekly: dict[str, list[tuple[str, str]]]) -> None:
        self.timezone = timezone
        self.weekly = weekly


SHANGHAI = Hours(
    "Asia/Shanghai",
    {"mon": [("09:00", "12:00"), ("13:30", "18:00")], "sat": [("10:00", "12:00")]},
)
NEW_YORK = Hours("America/New_York", {"mon": [("09:00", "17:00")]})


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def test_unconfigured_is_always_open() -> None:
    """未配置 = 7×24（§3.7）：2026-10-19 03:00 上海（周一，没配也是服务时间）。"""
    assert in_service_hours(None, _utc("2026-10-18T19:00:00")) is True


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        ("2026-10-19T01:00:00", True),  # 周一 09:00 上海：左闭
        ("2026-10-19T03:59:00", True),  # 11:59
        ("2026-10-19T04:00:00", False),  # 12:00：右开
        ("2026-10-19T05:30:00", True),  # 13:30 下午场左闭
        ("2026-10-19T10:00:00", False),  # 18:00：右开
        ("2026-10-19T00:59:00", False),  # 08:59 还没上班
        ("2026-10-20T02:00:00", False),  # 周二没配区间
        ("2026-10-24T03:00:00", True),  # 周六 11:00
    ],
)
def test_windows_are_half_open(moment: str, expected: bool) -> None:
    assert in_service_hours(SHANGHAI, _utc(moment)) is expected


def test_clock_face_follows_the_configured_timezone() -> None:
    """同一时刻在纽约是 09:30（配了窗口），在上海是 21:30（没配）。"""
    moment = _utc("2026-10-19T13:30:00")
    assert in_service_hours(NEW_YORK, moment) is True
    assert in_service_hours(SHANGHAI, moment) is False


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        ("2026-03-02T14:00:00", True),  # 3 月 2 日（EST，UTC-5）09:00 → 14:00Z
        ("2026-03-09T13:00:00", True),  # 3 月 9 日（EDT，UTC-4）09:00 → 13:00Z
        ("2026-03-09T12:59:00", False),  # 8:59 EDT：同一钟点，夏令时前后差一小时
    ],
)
def test_dst_uses_the_local_clock(moment: str, expected: bool) -> None:
    """夏令时切换（2026-03-08）以本地钟面为准：3 月 9 日的 09:00 对应 13:00Z，3 月 2 日对应 14:00Z。"""
    assert in_service_hours(NEW_YORK, _utc(moment)) is expected


def test_naive_datetime_is_treated_as_utc() -> None:
    assert in_service_hours(SHANGHAI, datetime.fromisoformat("2026-10-19T01:30:00")) is True


def test_malformed_windows_are_skipped() -> None:
    """配置侧已校验；畸形项按「不服务」兜底，扫描任务不会因为脏配置整体炸掉。"""
    cfg = Hours(
        "Asia/Shanghai",
        {"mon": [("25:00", "26:00"), ("09:00",), ("18:00", "09:00")]},  # type: ignore[list-item]
    )
    assert in_service_hours(cfg, _utc("2026-10-19T02:00:00")) is False  # 10:00 上海

    # 单数字小时照认（"9:00" → 09:00）：配置校验层已拦，这里只是宽进
    odd = Hours("Asia/Shanghai", {"mon": [("9:00", "18:00")]})
    assert in_service_hours(odd, _utc("2026-10-19T02:00:00")) is True
