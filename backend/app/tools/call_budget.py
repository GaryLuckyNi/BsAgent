"""同一图实例内原子预留搜索调用；恢复时从已记录调用重建使用量。"""

from threading import Lock
from collections.abc import Iterable


class SearchCallBudget:
    def __init__(self, *, limit: int) -> None:
        self.limit = limit
        self._lock = Lock()
        self._calls: dict[str, set[str]] = {}

    def reserve(self, run_key: str, call_id: str, *, existing: Iterable[str]) -> bool:
        with self._lock:
            calls = self._calls.setdefault(run_key, set())
            calls.update(existing)
            if call_id in calls:
                return True
            if len(calls) >= self.limit:
                return False
            calls.add(call_id)
            return True

    def release(self, run_key: str) -> None:
        with self._lock:
            self._calls.pop(run_key, None)
