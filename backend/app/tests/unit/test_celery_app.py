def test_celery_app_uses_json_and_reliable_worker_defaults():
    from app.worker.celery_app import celery_app

    assert celery_app.conf.task_serializer == "json"
    assert celery_app.conf.accept_content == ["json"]
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True
    assert celery_app.conf.worker_prefetch_multiplier == 1
    assert celery_app.conf.task_ignore_result is True
    assert "app.assets.tasks" in celery_app.conf.include
    assert celery_app.conf.task_routes["bs_agent.assets.purge_expired"] == {
        "queue": "maintenance"
    }
    assert celery_app.conf.beat_schedule["purge-expired-assets"]["task"] == (
        "bs_agent.assets.purge_expired"
    )
