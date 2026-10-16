"""Celery app。

启动（见 docs/06-development/02-开发环境搭建.md）：
  celery -A aics.tasks.celery_app worker -Q ingest,context,eval,maintenance --pool=threads -c 4 -l info
  celery -A aics.tasks.celery_app beat -l info
队列固定为 ingest / context / eval / maintenance（ADR-0003、ADR-0008）。beat 表随里程碑补充
（M1 cleanup、M2 idle_sweep 每 30 秒、M4 aggregate_daily_stats）。
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab
from kombu import Exchange, Queue

from aics.core.config import CELERY_QUEUES, get_settings
from aics.core.logging import configure_logging


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
    }
    return app


app = create_celery()
celery_app = app
