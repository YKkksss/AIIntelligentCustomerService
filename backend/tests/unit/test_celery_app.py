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
    from aics.cli import TASK_QUEUES
    from aics.tasks import ingest, maintenance  # noqa: F401
    from aics.tasks.celery_app import celery_app

    routes = celery_app.conf.task_routes
    for name in ("aics.reconcile", "aics.sweep_knowledge"):
        assert name in celery_app.tasks and routes[name]["queue"] == "maintenance"
    # CLI 不 import aics.tasks，按任务名投递：队列必须与 task_routes 一致
    assert {k: routes[k]["queue"] for k in TASK_QUEUES} == TASK_QUEUES
    beat = {v["task"] for v in celery_app.conf.beat_schedule.values()}
    assert {"aics.reconcile", "aics.sweep_knowledge"} <= beat


def test_cli_parses_reindex_and_reconcile(capsys: object) -> None:
    import pytest

    from aics.cli import main

    with pytest.raises(SystemExit):
        main(["reindex"])  # --app 必填
    assert main(["eval"]) == 2
