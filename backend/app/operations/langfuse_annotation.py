from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Self
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger("bs_agent.langfuse.annotation")
_QUEUE_CONFIGS: dict[str, tuple[str, ...]] = {
    "answer-quality-review": (
        "answer_quality",
        "groundedness",
        "action_policy_compliance",
        "review_outcome",
    ),
    "skill-tool-selection-review": ("selection_correctness",),
}


def _is_local_host(host: str) -> bool:
    normalized = host.rstrip(".").lower()
    return normalized in {
        "localhost",
        "127.0.0.1",
        "::1",
        "host.docker.internal",
        "langfuse-web",
    }


def _validate_base_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Langfuse base URL must be a valid local URL")
    if not _is_local_host(parsed.hostname):
        raise ValueError("Langfuse annotation API only accepts local base URLs")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Langfuse base URL must not contain credentials or query data")
    return base_url.rstrip("/")


def stable_sample(trace_id: str, rate: float) -> bool:
    if not 0 <= rate <= 1:
        raise ValueError("sample rate must be between 0 and 1")
    bucket = int.from_bytes(hashlib.sha256(trace_id.encode()).digest()[:8], "big")
    return bucket / (2**64 - 1) < rate


@dataclass(frozen=True)
class QueueRef:
    queue_id: str
    name: str
    status: str | None = None


@dataclass(frozen=True)
class AnnotationItem:
    item_id: str
    status: str | None = None


class AnnotationQueueClient:
    def __init__(
        self,
        base_url: str,
        public_key: str,
        secret_key: str,
        *,
        retry_attempts: int = 3,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 5.0,
        score_config_ids: Mapping[str, str] | None = None,
    ) -> None:
        self.base_url = _validate_base_url(base_url)
        self.retry_attempts = max(1, retry_attempts)
        self.score_config_ids = dict(score_config_ids or {})
        self._client = httpx.Client(
            base_url=self.base_url,
            auth=httpx.BasicAuth(public_key, secret_key),
            timeout=min(timeout, 5.0),
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Mapping[str, Any]:
        for attempt in range(self.retry_attempts):
            try:
                response = self._client.request(method, path, **kwargs)
            except (httpx.ConnectTimeout, httpx.ReadTimeout):
                if attempt + 1 == self.retry_attempts:
                    logger.warning("langfuse_annotation_request_failed error=timeout")
                    raise
                continue
            if response.status_code >= 500 and attempt + 1 < self.retry_attempts:
                continue
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, Mapping):
                raise TypeError("Langfuse annotation API returned an invalid response")
            return payload
        raise RuntimeError("Langfuse annotation request failed")

    @staticmethod
    def _data(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        data = payload.get("data", [])
        if isinstance(data, list):
            return [item for item in data if isinstance(item, Mapping)]
        return []

    def list_queues(self) -> list[QueueRef]:
        payload = self._request("GET", "/api/public/annotation-queues")
        values = self._data(payload)
        if not values and payload.get("id"):
            values = [payload]
        return [
            QueueRef(str(item["id"]), str(item.get("name", "")), _safe_status(item))
            for item in values
            if item.get("id")
        ]

    def ensure_queues(
        self, *, score_config_ids: Mapping[str, str] | None = None
    ) -> tuple[QueueRef, ...]:
        existing = {queue.name: queue for queue in self.list_queues()}
        configured_ids = dict(self.score_config_ids)
        if score_config_ids is not None:
            configured_ids.update(score_config_ids)
        result: list[QueueRef] = []
        for name, score_configs in _QUEUE_CONFIGS.items():
            queue = existing.get(name)
            if queue is None:
                missing_ids = [config for config in score_configs if not configured_ids.get(config)]
                if missing_ids:
                    raise ValueError(
                        "Langfuse score configuration IDs are required to create annotation queues"
                    )
                payload = self._request(
                    "POST",
                    "/api/public/annotation-queues",
                    json={
                        "name": name,
                        "description": name,
                        "scoreConfigIds": [configured_ids[config] for config in score_configs],
                    },
                )
                queue_id = payload.get("id")
                if not queue_id:
                    raise ValueError("Langfuse annotation queue response is missing an ID")
                queue = QueueRef(str(queue_id), name, _safe_status(payload))
            result.append(queue)
        return tuple(result)

    def enqueue(
        self,
        queue_name: str,
        *,
        business_trace_id: str,
        object_id: str,
        reason: str,
        object_type: str = "TRACE",
        metadata: Mapping[str, Any] | None = None,
        force: bool = False,
    ) -> AnnotationItem | None:
        if queue_name not in _QUEUE_CONFIGS:
            raise ValueError("Unknown Langfuse annotation queue")
        queue = next((item for item in self.list_queues() if item.name == queue_name), None)
        if queue is None:
            queue = next(item for item in self.ensure_queues() if item.name == queue_name)
        # The public API uses one-based page numbers and reports totalPages in meta.
        path = f"/api/public/annotation-queues/{queue.queue_id}/items"
        page = 1
        while True:
            params = {"limit": 100, "page": page}
            payload = self._request("GET", path, params=params)
            for item in self._data(payload):
                item_object_id = item.get("objectId") or item.get("object_id")
                if (
                    str(item_object_id) == object_id
                    and item.get("objectType") == object_type
                    and item.get("id")
                ):
                    return AnnotationItem(str(item["id"]), _safe_status(item))
            meta = payload.get("meta")
            current_page = meta.get("page") if isinstance(meta, Mapping) else None
            total_pages = meta.get("totalPages") if isinstance(meta, Mapping) else None
            if (
                not isinstance(current_page, int)
                or isinstance(current_page, bool)
                or current_page < 1
                or not isinstance(total_pages, int)
                or isinstance(total_pages, bool)
                or total_pages < current_page
            ):
                break
            if current_page >= total_pages:
                break
            page = current_page + 1
        if not force and not reason:
            return None
        payload = self._request(
            "POST",
            path,
            json={
                "objectId": object_id,
                "objectType": object_type,
            },
        )
        item_id = payload.get("id")
        if not item_id:
            raise ValueError("Langfuse annotation item response is missing an ID")
        return AnnotationItem(str(item_id), _safe_status(payload))

    def update_item(
        self, queue_id: str, item_id: str, *, status: str, comment: str | None = None
    ) -> AnnotationItem:
        payload = self._request(
            "PATCH",
            f"/api/public/annotation-queues/{queue_id}/items/{item_id}",
            json={"status": status},
        )
        returned_id = payload.get("id", item_id)
        return AnnotationItem(str(returned_id), _safe_status(payload))


def _safe_status(payload: Mapping[str, Any]) -> str | None:
    value = payload.get("status")
    return str(value) if value is not None else None


@dataclass(frozen=True)
class ReviewPolicy:
    answer_threshold: float = 0.7
    selection_confidence_threshold: float = 0.6
    sample_rate: float = 0.05

    def answer_reasons(
        self,
        *,
        business_trace_id: str,
        scores: Mapping[str, Any],
        rule_judge_disagreement: bool,
        failed: bool,
        write_operation: bool,
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        for key in ("answer_quality", "groundedness"):
            value = scores.get(key)
            if isinstance(value, (int, float)) and value < self.answer_threshold:
                reasons.append("low_score")
                break
        if rule_judge_disagreement:
            reasons.append("rule_judge_disagreement")
        if failed:
            reasons.append("failure")
        if write_operation:
            reasons.append("write_operation")
        if stable_sample(business_trace_id, self.sample_rate):
            reasons.append("sampled")
        return tuple(dict.fromkeys(reasons))

    def selection_reasons(self, *, business_trace_id: str, confidence: float) -> tuple[str, ...]:
        reasons = []
        if confidence < self.selection_confidence_threshold:
            reasons.append("low_selection_confidence")
        if stable_sample(business_trace_id, self.sample_rate):
            reasons.append("sampled")
        return tuple(reasons)
