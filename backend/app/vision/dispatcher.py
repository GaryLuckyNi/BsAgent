from typing import Any, Protocol

from app.assets.service import AssetAnalysisDispatchRequest


class CeleryApp(Protocol):
    def send_task(self, name: str, **kwargs: Any) -> Any: ...


class ImageAnalysisDispatchError(RuntimeError):
    code = "analysis_dispatch_failed"


class CeleryImageAnalysisDispatcher:
    def __init__(
        self,
        *,
        celery_app: CeleryApp,
        task_name: str = "bs_agent.vision.analyze_asset",
        queue: str = "image-analysis",
    ) -> None:
        self._celery_app = celery_app
        self._task_name = task_name
        self._queue = queue

    def dispatch(self, request: AssetAnalysisDispatchRequest) -> None:
        try:
            self._celery_app.send_task(
                self._task_name,
                kwargs={
                    "asset_id": request.asset_id,
                    "analysis_id": request.analysis_id,
                    "tenant_id": request.tenant_id,
                    "user_id": request.user_id,
                    "session_id": request.session_id,
                    "trace_id": request.trace_id,
                },
                queue=self._queue,
            )
        except Exception as exc:
            raise ImageAnalysisDispatchError("图片分析任务派发失败。") from exc
