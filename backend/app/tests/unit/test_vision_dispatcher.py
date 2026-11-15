from app.assets.service import AssetAnalysisDispatchRequest
from app.vision.dispatcher import CeleryImageAnalysisDispatcher


class RecordingCeleryApp:
    def __init__(self):
        self.calls = []

    def send_task(self, name, **kwargs):
        self.calls.append((name, kwargs))


def test_celery_dispatcher_sends_only_trusted_asset_references():
    app = RecordingCeleryApp()
    dispatcher = CeleryImageAnalysisDispatcher(celery_app=app)

    dispatcher.dispatch(
        AssetAnalysisDispatchRequest(
            asset_id="asset-1",
            analysis_id="analysis-1",
            tenant_id="tenant-1",
            user_id="user-1",
            session_id="session-1",
            object_key="private/should-not-leave-api.png",
            media_type="image/png",
            trace_id="trace-1",
        )
    )

    task_name, options = app.calls[0]
    assert task_name == "bs_agent.vision.analyze_asset"
    assert options["queue"] == "image-analysis"
    assert options["kwargs"] == {
        "asset_id": "asset-1",
        "analysis_id": "analysis-1",
        "tenant_id": "tenant-1",
        "user_id": "user-1",
        "session_id": "session-1",
        "trace_id": "trace-1",
    }
    assert "object_key" not in options["kwargs"]
    assert "media_type" not in options["kwargs"]
