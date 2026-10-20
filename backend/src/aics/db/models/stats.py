"""每日统计表（数据库设计 §3.12.1，迁移 `0015`，M4.6 ②）。

一天一行、一个应用一行：主键 `(app_id, date)`，`date` 是 **Asia/Shanghai** 的自然日。所有计数列
都由 `modules.stats` 的聚合任务写入，接口只读这张表，不实时扫 `messages`（性能与口径可预期）。
计数列默认 0；分位数列为 NULL 表示当天没有可用样本（例如 `first_token_p95_ms` 只覆盖流式轮次）。
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Integer, func
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from aics.db.base import Base

#: 统计只覆盖这两个渠道；`debug` / `eval` 是内部流量（数据库设计 §3.12.1）。
STATS_CHANNELS = ("api", "openai")

COUNT_COLUMNS = (
    "conversations",
    "messages",
    "answered",
    "refused",
    "handoff",
    "errors",
    "handoff_requested",
    "handoff_accepted",
    "handoff_released",
    "handoff_queue_timeout",
    "handoff_offline",
    "handoff_accepted_idle_closed",
    "human_agent_messages",
    "idle_closed",
    "negative_feedback",
)


class AppDailyStats(Base):
    __tablename__ = "app_daily_stats"

    app_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("apps.id", ondelete="CASCADE"),
        primary_key=True,
    )
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    conversations: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    messages: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    answered: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    refused: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    handoff: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    errors: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    handoff_requested: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sa_text("0")
    )
    handoff_accepted: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sa_text("0")
    )
    handoff_released: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sa_text("0")
    )
    # pending → accepted 的耗时中位数（秒），当天没有接单时为 NULL。
    handoff_accept_p50_s: Mapped[int | None] = mapped_column(Integer)
    handoff_queue_timeout: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sa_text("0")
    )
    handoff_offline: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sa_text("0")
    )
    handoff_accepted_idle_closed: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sa_text("0")
    )
    human_agent_messages: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sa_text("0")
    )
    idle_closed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sa_text("0"))
    negative_feedback: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=sa_text("0")
    )
    intent_distribution: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa_text("'{}'::jsonb")
    )
    tokens_in: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=sa_text("0"))
    tokens_out: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=sa_text("0"))
    latency_p50_ms: Mapped[int | None] = mapped_column(Integer)
    latency_p95_ms: Mapped[int | None] = mapped_column(Integer)
    # 只有流式轮次会写 messages.first_token_ms（ISSUE-93），口径见管理接口参考。
    first_token_p95_ms: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"AppDailyStats(app_id={self.app_id!r}, date={self.date!r})"
