import pytest

from app.vision.lock import AnalysisLockBusyError, RedisAnalysisLock


class RecordingLock:
    def __init__(self, *, acquired=True):
        self.acquired = acquired
        self.released = False

    def acquire(self, *, blocking, blocking_timeout):
        assert blocking is True
        assert blocking_timeout == 1.5
        return self.acquired

    def release(self):
        self.released = True


class RecordingRedis:
    def __init__(self, lock):
        self.lock_instance = lock
        self.calls = []

    def lock(self, name, *, timeout, thread_local):
        self.calls.append((name, timeout, thread_local))
        return self.lock_instance


def test_redis_analysis_lock_is_namespaced_and_released():
    lock = RecordingLock()
    redis_client = RecordingRedis(lock)
    analysis_lock = RedisAnalysisLock(
        redis_client=redis_client,
        timeout_seconds=300,
        blocking_timeout_seconds=1.5,
    )

    with analysis_lock.acquire(tenant_id="tenant-1", analysis_id="analysis-1"):
        pass

    name, timeout, thread_local = redis_client.calls[0]
    assert name.startswith("bs-agent:image-analysis:")
    assert "tenant-1" not in name
    assert name.endswith(":analysis-1")
    assert timeout == 300
    assert thread_local is False
    assert lock.released is True


def test_redis_analysis_lock_reports_busy_without_running_body():
    lock = RecordingLock(acquired=False)
    analysis_lock = RedisAnalysisLock(
        redis_client=RecordingRedis(lock),
        blocking_timeout_seconds=1.5,
    )

    with pytest.raises(AnalysisLockBusyError):
        with analysis_lock.acquire(
            tenant_id="tenant-1",
            analysis_id="analysis-1",
        ):
            pytest.fail("锁未获取时不得执行任务体")
