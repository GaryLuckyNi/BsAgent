"""同一图实例内原子预留搜索调用；恢复时从已记录调用重建使用量。"""

from collections.abc import Iterable
from threading import Lock


def consumes_search_budget(record) -> bool:
    result = record.result
    return bool(record.request.tool_name == "web_search" and result is not None and not (
        result.error and (result.error.code == "research_budget_exhausted" or (
            result.error.code == "tool_argument_validation_failed" and result.execution_state == "not_started"
        ))
    ))


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
