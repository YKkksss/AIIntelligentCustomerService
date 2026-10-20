"""统计任务（队列 `maintenance`）：每小时重算当天，00:30 定稿前一天（M4.6 ②）。

`aggregate_daily_stats()` 不带参数时重算「今天」（Asia/Shanghai）；`yesterday=True` 重算前一天，
供 00:30 的 beat 定稿。两种模式都是 `INSERT … ON CONFLICT DO UPDATE`，重复执行结果一致。
"""

from __future__ import annotations

import asyncio
from typing import Any

from aics.tasks.celery_app import celery_app

STATS_TASK = "aics.aggregate_daily_stats"


@celery_app.task(name=STATS_TASK, queue="maintenance")
def aggregate_daily_stats(day: str | None = None, *, yesterday: bool = False) -> dict[str, Any]:
    """重算指定日（缺省今天；`yesterday=True` 为前一天）的全部应用统计。"""
    from aics.modules.stats.service import run_aggregate_job

    return asyncio.run(run_aggregate_job(day, yesterday=yesterday))
