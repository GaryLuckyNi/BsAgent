from functools import lru_cache
from threading import BoundedSemaphore
from typing import Any

import pybreaker
from redis import Redis
from sqlalchemy.orm import Session

from app.assets.factory import build_object_store
from app.assets.object_store import ObjectStore
from app.assets.service import ImageAnalysisDispatcher, NoopImageAnalysisDispatcher
from app.core.config import Settings, settings
from app.vision.client import OpenAICompatibleVisionClient, VisionProviderError
from app.vision.coordinator import ImageAnalysisCoordinator
from app.vision.dispatcher import CeleryImageAnalysisDispatcher
from app.vision.lock import RedisAnalysisLock
from app.vision.ocr_client import OcrProviderError, PaddleOcrHttpClient
from app.vision.policy import SqlAlchemyTenantImagePolicyProvider
from app.vision.processor import VisionAnalysisProcessor
from app.vision.resilience import ProviderCallGuard


def build_provider_call_guard(
    *,
    provider_name: str,
    config: Settings | None = None,
    redis_client: Redis | None = None,
) -> ProviderCallGuard:
    runtime = config or settings
    if runtime is settings and redis_client is None:
        return _build_default_provider_call_guard(provider_name)
    selected_redis = redis_client or Redis.from_url(runtime.redis_url)
    storage = pybreaker.CircuitRedisStorage(
        pybreaker.STATE_CLOSED,
        selected_redis,
        namespace=f"bs-agent:image-provider:{provider_name}",
    )
    breaker = pybreaker.CircuitBreaker(
        name=f"image-provider-{provider_name}",
        fail_max=runtime.provider_circuit_fail_max,
        reset_timeout=runtime.provider_circuit_reset_seconds,
        state_storage=storage,
        exclude=[_exclude_non_transient_provider_error],
    )
    return ProviderCallGuard(
        provider_name=provider_name,
        circuit_breaker=breaker,
        semaphore=BoundedSemaphore(runtime.provider_max_concurrency),
        acquire_timeout_seconds=runtime.provider_concurrency_wait_seconds,
    )


@lru_cache(maxsize=2)
def _build_default_provider_call_guard(provider_name: str) -> ProviderCallGuard:
    return build_provider_call_guard(
        provider_name=provider_name,
        config=settings,
        redis_client=Redis.from_url(settings.redis_url),
    )


def _exclude_non_transient_provider_error(exc: BaseException) -> bool:
    if not isinstance(exc, (OcrProviderError, VisionProviderError)):
        return False
    if exc.code.endswith(("_timeout", "_unreachable")):
        return False
    if exc.code.endswith(("_circuit_open", "_concurrency_limited")):
        return False
    return not (
        exc.code.endswith("_http_error")
        and exc.status_code in {429, 500, 502, 503, 504}
    )


def build_vision_client(
    *,
    config: Settings | None = None,
) -> OpenAICompatibleVisionClient:
    runtime = config or settings
    if runtime.vision_provider != "openai_compatible":
        raise ValueError("未配置可用的 Vision provider。")
    return OpenAICompatibleVisionClient(
        api_key=runtime.vision_api_key,
        model=runtime.vision_model,
        base_url=runtime.vision_base_url,
        timeout_seconds=runtime.vision_timeout_seconds,
        max_output_tokens=runtime.vision_max_output_tokens,
        call_guard=build_provider_call_guard(
            provider_name="vision",
            config=runtime,
        ),
    )


def build_image_analysis_dispatcher(
    *,
    config: Settings | None = None,
    celery_app: Any | None = None,
) -> ImageAnalysisDispatcher:
    runtime = config or settings
    if runtime.image_analysis_dispatcher_backend == "noop":
        return NoopImageAnalysisDispatcher()
    if celery_app is None:
        from app.worker.celery_app import celery_app as default_celery_app

        celery_app = default_celery_app
    return CeleryImageAnalysisDispatcher(celery_app=celery_app)


def build_ocr_client(
    *,
    config: Settings | None = None,
) -> PaddleOcrHttpClient:
    runtime = config or settings
    if runtime.ocr_provider != "paddleocr_http":
        raise ValueError("未配置可用的 OCR provider。")
    return PaddleOcrHttpClient(
        base_url=runtime.ocr_base_url,
        provider_version=runtime.ocr_provider_version,
        api_key=runtime.ocr_api_key,
        timeout_seconds=runtime.ocr_timeout_seconds,
        call_guard=build_provider_call_guard(
            provider_name="ocr",
            config=runtime,
        ),
    )


def build_vision_processor(
    *,
    session: Session,
    config: Settings | None = None,
    object_store: ObjectStore | None = None,
    analysis_coordinator: ImageAnalysisCoordinator | None = None,
) -> VisionAnalysisProcessor:
    runtime = config or settings
    coordinator = analysis_coordinator or ImageAnalysisCoordinator(
        ocr_client=build_ocr_client(config=runtime),
        vision_client=build_vision_client(config=runtime),
        policy_provider=SqlAlchemyTenantImagePolicyProvider(session),
    )
    return VisionAnalysisProcessor(
        session=session,
        object_store=object_store or build_object_store(config=runtime),
        analysis_coordinator=coordinator,
    )


@lru_cache(maxsize=1)
def build_analysis_lock() -> RedisAnalysisLock:
    return RedisAnalysisLock(
        redis_client=Redis.from_url(settings.redis_url),
        timeout_seconds=settings.image_analysis_lock_ttl_seconds,
        blocking_timeout_seconds=settings.image_analysis_lock_wait_seconds,
    )
