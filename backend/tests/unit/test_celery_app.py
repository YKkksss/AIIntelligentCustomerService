def test_queues_declared() -> None:
    from aics.tasks.celery_app import create_celery

    app = create_celery()
    assert [q.name for q in app.conf.task_queues] == ["ingest", "context", "eval", "maintenance"]
    assert app.conf.task_acks_late is True
    assert app.conf.task_default_queue == "maintenance"


def test_ingest_tasks_registered_and_routed() -> None:
    from aics.tasks import ingest, maintenance  # noqa: F401
    from aics.tasks.celery_app import celery_app

    assert {"aics.ingest_document", "aics.cleanup_document"} <= set(celery_app.tasks)
    routes = celery_app.conf.task_routes
    assert routes["aics.ingest_document"]["queue"] == "ingest"
    assert routes["aics.cleanup_document"]["queue"] == "maintenance"
    assert routes["aics.retokenize_app"]["queue"] == "ingest"
    assert "aics.retokenize_app" in celery_app.tasks
    assert "aics.tasks.ingest" in celery_app.conf.include


def test_maintenance_tasks_beat_and_cli_queue_map() -> None:
    from aics.cli import _task_queues
    from aics.modules.knowledge.service import TASK_QUEUES
    from aics.tasks import ingest, maintenance  # noqa: F401
    from aics.tasks.celery_app import celery_app

    routes = celery_app.conf.task_routes
    for name in ("aics.reconcile", "aics.sweep_knowledge"):
        assert name in celery_app.tasks and routes[name]["queue"] == "maintenance"
    # CLI 不 import aics.tasks，按任务名投递：队列映射的唯一来源是 knowledge 服务的 TASK_QUEUES
    assert _task_queues() is TASK_QUEUES
    assert {k: routes[k]["queue"] for k in TASK_QUEUES} == TASK_QUEUES
    beat = {v["task"] for v in celery_app.conf.beat_schedule.values()}
    assert {"aics.reconcile", "aics.sweep_knowledge"} <= beat


def test_cli_parses_reindex_and_reconcile(capsys: object) -> None:
    import pytest

    from aics.cli import main

    with pytest.raises(SystemExit):
        main(["reindex"])  # --app 必填
    assert main(["eval"]) == 2


def test_idle_sweep_task_routed_and_scheduled_every_30s() -> None:
    from aics.modules.chat import lifecycle
    from aics.tasks import maintenance  # noqa: F401
    from aics.tasks.celery_app import IDLE_SWEEP_INTERVAL_S, celery_app

    name = lifecycle.IDLE_SWEEP_TASK
    assert name in celery_app.tasks
    assert celery_app.conf.task_routes[name]["queue"] == "maintenance"
    entry = celery_app.conf.beat_schedule["idle-sweep"]
    assert entry["task"] == name
    assert entry["schedule"] == IDLE_SWEEP_INTERVAL_S == lifecycle.IDLE_SWEEP_INTERVAL_S == 30
    assert 0 < entry["options"]["expires"] < entry["schedule"]


def test_idle_sweep_task_and_cli_call_the_jobs(monkeypatch: object, capsys: object) -> None:
    import pytest

    from aics.cli import main
    from aics.modules.chat import lifecycle
    from aics.modules.handoff import sweep
    from aics.tasks import maintenance

    lifecycle_out = {"apps": 1, "noticed": 2, "closed": 3, "skipped_apps": 0}
    handoff_out = {"apps": 1, "queue_timeouts": 4, "accepted_idle_closed": 0, "skipped_apps": 0}

    async def fake_lifecycle(settings: object = None) -> dict:
        return lifecycle_out

    async def fake_handoff(settings: object = None) -> dict:
        return handoff_out

    mp: pytest.MonkeyPatch = monkeypatch  # type: ignore[assignment]
    mp.setattr(maintenance, "run_idle_sweep_job", fake_lifecycle)
    mp.setattr(maintenance, "run_handoff_sweep_job", fake_handoff)
    mp.setattr(lifecycle, "run_idle_sweep_job", fake_lifecycle)
    mp.setattr(sweep, "run_handoff_sweep_job", fake_handoff)
    # §3.10 顺序：转人工超时先跑，再跑空闲提醒与关闭
    assert maintenance.idle_sweep() == {"handoff": handoff_out, "lifecycle": lifecycle_out}
    assert main(["sweep-idle"]) == 0
    out = capsys.readouterr().out  # type: ignore[attr-defined]
    assert '"closed": 3' in out and '"queue_timeouts": 4' in out


def test_key_usage_flush_task_routed_and_scheduled_every_60s() -> None:
    from aics.modules.apikeys import keyusage
    from aics.tasks import maintenance  # noqa: F401
    from aics.tasks.celery_app import KEY_USAGE_FLUSH_INTERVAL_S, celery_app

    name = keyusage.KEY_USAGE_FLUSH_TASK
    assert name in celery_app.tasks
    assert celery_app.conf.task_routes[name]["queue"] == "maintenance"
    entry = celery_app.conf.beat_schedule["flush-key-usage"]
    assert entry["task"] == name
    assert entry["schedule"] == KEY_USAGE_FLUSH_INTERVAL_S == 60
    assert entry["schedule"] == keyusage.KEY_USAGE_FLUSH_INTERVAL_S
    assert 0 < entry["options"]["expires"] < entry["schedule"]


def test_key_usage_flush_task_calls_the_job(monkeypatch: object) -> None:
    import pytest

    from aics.tasks import maintenance

    out = {"keys": 3, "buckets": 2}

    async def fake_job(settings: object = None) -> dict:
        return out

    mp: pytest.MonkeyPatch = monkeypatch  # type: ignore[assignment]
    mp.setattr(maintenance, "run_key_usage_flush_job", fake_job)
    assert maintenance.flush_key_usage() == out
