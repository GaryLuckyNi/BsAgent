from collections.abc import Callable
from typing import Any, Protocol, TypeVar

import pybreaker


T = TypeVar("T")


class Semaphore(Protocol):
    def acquire(self, *, blocking: bool, timeout: float) -> bool: ...

    def release(self) -> None: ...


class ProviderCircuitOpenError(RuntimeError):
    def __init__(self, *, provider_name: str) -> None:
        self.code = f"{provider_name}_circuit_open"
        super().__init__("上游图片分析服务熔断器已打开。")


class ProviderConcurrencyLimitError(RuntimeError):
    def __init__(self, *, provider_name: str) -> None:
        self.code = f"{provider_name}_concurrency_limited"
        super().__init__("上游图片分析服务当前并发已满。")


class ProviderCallGuard:
    def __init__(
        self,
        *,
        provider_name: str,
        circuit_breaker: Any,
        semaphore: Semaphore,
        acquire_timeout_seconds: float = 0.5,
    ) -> None:
        self._provider_name = provider_name
        self._circuit_breaker = circuit_breaker
        self._semaphore = semaphore
        self._acquire_timeout_seconds = acquire_timeout_seconds

    def call(self, operation: Callable[[], T]) -> T:
        acquired = self._semaphore.acquire(
            blocking=True,
            timeout=self._acquire_timeout_seconds,
        )
        if not acquired:
            raise ProviderConcurrencyLimitError(
                provider_name=self._provider_name
            )
        try:
            try:
                return self._circuit_breaker.call(operation)
            except pybreaker.CircuitBreakerError as exc:
                raise ProviderCircuitOpenError(
                    provider_name=self._provider_name
                ) from exc
        finally:
            self._semaphore.release()
