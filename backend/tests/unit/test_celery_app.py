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
