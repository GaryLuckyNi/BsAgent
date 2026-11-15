import pybreaker
import pytest

from app.vision.resilience import (
    ProviderCallGuard,
    ProviderCircuitOpenError,
    ProviderConcurrencyLimitError,
)


class RecordingSemaphore:
    def __init__(self, *, acquired=True):
        self.acquired = acquired
        self.released = False
        self.timeout = None

    def acquire(self, *, blocking, timeout):
        assert blocking is True
        self.timeout = timeout
        return self.acquired

    def release(self):
        self.released = True


class RecordingBreaker:
    def __init__(self, *, open_circuit=False):
        self.open_circuit = open_circuit
        self.calls = 0

    def call(self, operation):
        self.calls += 1
        if self.open_circuit:
            raise pybreaker.CircuitBreakerError("internal breaker detail")
        return operation()


def test_provider_guard_rejects_when_concurrency_bulkhead_is_full():
    semaphore = RecordingSemaphore(acquired=False)
    breaker = RecordingBreaker()
    guard = ProviderCallGuard(
        provider_name="vision",
        circuit_breaker=breaker,
        semaphore=semaphore,
        acquire_timeout_seconds=0.25,
    )

    with pytest.raises(ProviderConcurrencyLimitError) as exc_info:
        guard.call(lambda: pytest.fail("并发受限时不得调用 Provider"))

    assert exc_info.value.code == "vision_concurrency_limited"
    assert semaphore.timeout == 0.25
    assert breaker.calls == 0


def test_provider_guard_maps_open_circuit_and_releases_bulkhead():
    semaphore = RecordingSemaphore()
    guard = ProviderCallGuard(
        provider_name="ocr",
        circuit_breaker=RecordingBreaker(open_circuit=True),
        semaphore=semaphore,
        acquire_timeout_seconds=0.1,
    )

    with pytest.raises(ProviderCircuitOpenError) as exc_info:
        guard.call(lambda: "unused")

    assert exc_info.value.code == "ocr_circuit_open"
    assert "internal breaker detail" not in str(exc_info.value)
    assert semaphore.released is True


def test_provider_guard_releases_bulkhead_after_provider_failure():
    semaphore = RecordingSemaphore()
    guard = ProviderCallGuard(
        provider_name="vision",
        circuit_breaker=RecordingBreaker(),
        semaphore=semaphore,
    )

    with pytest.raises(ValueError):
        guard.call(lambda: (_ for _ in ()).throw(ValueError("failed")))

    assert semaphore.released is True
