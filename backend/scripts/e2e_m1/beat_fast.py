"""短时 beat 验证：沿用正式 celery_app 的路由与 broker，只把两个定时任务改成每 20 秒一次。

用法：`uv run celery -A scripts.e2e_m1.beat_fast beat -s /tmp/e2e/beat-schedule`（运行约 70 秒后停止），
然后在 worker 日志中确认 aics.sweep_knowledge / aics.reconcile 被执行。
"""

from aics.tasks.celery_app import celery_app

celery_app.conf.beat_schedule = {
    name: {**entry, "schedule": 20.0} for name, entry in celery_app.conf.beat_schedule.items()
}
app = celery_app
