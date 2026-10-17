"""Celery app。

启动（见 docs/06-development/02-开发环境搭建.md）：
  celery -A aics.tasks.celery_app worker -Q ingest,context,eval,maintenance --pool=threads -c 4 -l info
  celery -A aics.tasks.celery_app beat -l info
队列固定为 ingest / context / eval / maintenance（ADR-0003、ADR-0008）。beat 表随里程碑补充
（M1 cleanup、M2 idle_sweep 每 30 秒、M3.1 flush_key_usage 每 60 秒、M4 aggregate_daily_stats）。
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab
from kombu import Exchange, Queue

from aics.core.config import CELERY_QUEUES, get_settings
from aics.core.logging import configure_logging

IDLE_SWEEP_INTERVAL_S = 30.0  # = chat.lifecycle.IDLE_SWEEP_INTERVAL_S (tests keep them equal)
KEY_USAGE_FLUSH_INTERVAL_S = 60.0  # = apikeys.keyusage.KEY_USAGE_FLUSH_INTERVAL_S


def create_celery() -> Celery:
    s = get_settings()
    configure_logging(s.log_level, json=s.log_json or s.is_prod)
    app = Celery("aics", broker=s.celery_broker_url, backend=s.celery_result_backend)
    exchange = Exchange("aics", type="direct")
    app.conf.update(
        task_queues=[Queue(q, exchange, routing_key=q, durable=True) for q in CELERY_QUEUES],
        task_default_queue="maintenance",
        task_default_exchange="aics",
        task_default_routing_key="maintenance",
        task_create_missing_queues=False,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        result_expires=3600,
        timezone="Asia/Shanghai",
        enable_utc=True,
        worker_hijack_root_logger=False,
        beat_schedule={
            # M1：每小时兜底清理、每日对账（时区 Asia/Shanghai）
            "sweep-knowledge-hourly": {
                "task": "aics.sweep_knowledge",
                "schedule": crontab(minute=17),
            },
            "reconcile-daily": {"task": "aics.reconcile", "schedule": crontab(hour=3, minute=30)},
            # M2.7：空闲提醒 / 关闭（会话生命周期 §2.2）。expires：worker 积压时丢弃过期的
            # 一轮，不排队补跑（下一轮会处理同样的会话，条件更新保证幂等）。
            "idle-sweep": {
                "task": "aics.idle_sweep",
                "schedule": IDLE_SWEEP_INTERVAL_S,
                "options": {"expires": IDLE_SWEEP_INTERVAL_S - 5},
            },
            # M3.1：Key 最近使用时间（Redis 分桶 → api_keys.last_used_at）。丢掉一轮不清零：
            # 下一轮读到同一批未过期的桶，桶里保留的是最大时间戳。
            "flush-key-usage": {
                "task": "aics.flush_key_usage",
                "schedule": KEY_USAGE_FLUSH_INTERVAL_S,
                "options": {"expires": KEY_USAGE_FLUSH_INTERVAL_S - 5},
            },
        },
        broker_connection_retry_on_startup=True,
    )
    # 任务模块随里程碑加入
    app.conf.include = ["aics.tasks.ingest", "aics.tasks.maintenance", "aics.tasks.context"]
    app.conf.task_routes = {
        "aics.ingest_document": {"queue": "ingest", "routing_key": "ingest"},
        "aics.retokenize_app": {"queue": "ingest", "routing_key": "ingest"},
        "aics.cleanup_document": {"queue": "maintenance", "routing_key": "maintenance"},
        "aics.reconcile": {"queue": "maintenance", "routing_key": "maintenance"},
        "aics.sweep_knowledge": {"queue": "maintenance", "routing_key": "maintenance"},
        "aics.compact_conversation": {"queue": "context", "routing_key": "context"},
        "aics.idle_sweep": {"queue": "maintenance", "routing_key": "maintenance"},
        "aics.flush_key_usage": {"queue": "maintenance", "routing_key": "maintenance"},
    }
    return app


app = create_celery()
celery_app = app
