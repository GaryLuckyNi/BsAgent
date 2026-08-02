from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256
from typing import Any

from redis.exceptions import LockNotOwnedError


class AnalysisLockBusyError(RuntimeError):
    code = "analysis_lock_busy"


class AnalysisLockLostError(RuntimeError):
    code = "analysis_lock_lost"


class RedisAnalysisLock:
    def __init__(
        self,
        *,
        redis_client: Any,
        timeout_seconds: int = 300,
        blocking_timeout_seconds: float = 1.5,
    ) -> None:
        self._redis_client = redis_client
        self._timeout_seconds = timeout_seconds
        self._blocking_timeout_seconds = blocking_timeout_seconds

    @contextmanager
    def acquire(self, *, tenant_id: str, analysis_id: str) -> Iterator[None]:
        tenant_namespace = sha256(tenant_id.encode("utf-8")).hexdigest()[:24]
        name = f"bs-agent:image-analysis:{tenant_namespace}:{analysis_id}"
        lock = self._redis_client.lock(
            name,
            timeout=self._timeout_seconds,
            thread_local=False,
        )
        acquired = lock.acquire(
            blocking=True,
            blocking_timeout=self._blocking_timeout_seconds,
        )
        if not acquired:
            raise AnalysisLockBusyError("图片分析任务正在由其他 Worker 执行。")
        try:
            yield
        finally:
            try:
                lock.release()
            except LockNotOwnedError as exc:
                raise AnalysisLockLostError("图片分析任务锁已失效。") from exc
