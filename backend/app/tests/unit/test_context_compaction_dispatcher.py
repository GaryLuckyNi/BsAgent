from app.context_compaction.dispatcher import ContextCompactionDispatcher


class RecordingCeleryApp:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = []

    def send_task(self, name, **options):
        self.calls.append((name, options))
        if self.fail:
            raise RuntimeError("broker unavailable")


def test_dispatcher_sends_only_owner_identity_to_dedicated_queue():
    celery_app = RecordingCeleryApp()

    dispatched = ContextCompactionDispatcher(celery_app=celery_app).dispatch(
        tenant_id="tenant-1",
        user_id="user-1",
        session_key="session-1",
    )

    assert dispatched is True
    assert celery_app.calls == [
        (
            "bs_agent.context_compaction.compact_session",
            {
                "kwargs": {
                    "tenant_id": "tenant-1",
                    "user_id": "user-1",
                    "session_key": "session-1",
                },
                "queue": "context-compaction",
            },
        )
    ]


def test_dispatcher_returns_false_when_broker_is_unavailable():
    celery_app = RecordingCeleryApp(fail=True)

    dispatched = ContextCompactionDispatcher(celery_app=celery_app).dispatch(
        tenant_id="tenant-1",
        user_id="user-1",
        session_key="session-1",
    )

    assert dispatched is False
