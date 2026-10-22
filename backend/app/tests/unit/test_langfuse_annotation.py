from __future__ import annotations

import httpx
import pytest
from app.operations.langfuse_annotation import (
    AnnotationQueueClient,
    ReviewPolicy,
    stable_sample,
)

BASE = "http://localhost:3000"


def test_stable_sample_uses_sha256_bucket_formula() -> None:
    import hashlib

    trace_id = "business-123"
    bucket = int.from_bytes(hashlib.sha256(trace_id.encode()).digest()[:8], "big")
    expected = bucket / (2**64 - 1) < 0.05
    assert stable_sample(trace_id, 0.05) is expected
    assert stable_sample(trace_id, 1.0)
    assert not stable_sample(trace_id, 0.0)


def test_rejects_remote_base_url() -> None:
    with pytest.raises(ValueError):
        AnnotationQueueClient("https://cloud.langfuse.com", "pk", "sk")


@pytest.mark.parametrize(
    "host",
    ["http://langfuse:3000", "http://redis:6379", "http://127.0.0.2:3000"],
)
def test_rejects_unapproved_single_label_hosts(host: str) -> None:
    with pytest.raises(ValueError, match="local base URLs"):
        AnnotationQueueClient(host, "pk", "sk")


def test_accepts_explicit_compose_langfuse_host() -> None:
    client = AnnotationQueueClient("http://langfuse-web:3000", "pk", "sk")
    client.close()


def test_ensures_two_queues_with_expected_score_configs_and_safe_results() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers.get("authorization", "").startswith("Basic ")
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(
            201,
            json={
                "id": f"q{len(requests)}",
                "name": "queue",
                "status": "ACTIVE",
                "secret": "never-return",
            },
        )

    client = AnnotationQueueClient(BASE, "public", "secret", transport=httpx.MockTransport(handler))
    score_ids = {
        "answer_quality": "score-1",
        "groundedness": "score-2",
        "action_policy_compliance": "score-3",
        "review_outcome": "score-4",
        "selection_correctness": "score-5",
    }
    queues = client.ensure_queues(score_config_ids=score_ids)
    assert [q.name for q in queues] == ["answer-quality-review", "skill-tool-selection-review"]
    assert requests[0].url.path == "/api/public/annotation-queues"
    import json

    payloads = [json.loads(r.content) for r in requests if r.method == "POST"]
    assert payloads[0] == {
        "name": "answer-quality-review",
        "description": "answer-quality-review",
        "scoreConfigIds": ["score-1", "score-2", "score-3", "score-4"],
    }
    assert payloads[1] == {
        "name": "skill-tool-selection-review",
        "description": "skill-tool-selection-review",
        "scoreConfigIds": ["score-5"],
    }
    assert "secret" not in repr(queues)
    client.close()


def test_enqueue_queries_existing_items_and_deduplicates_by_object_id() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path.endswith("annotation-queues"):
            return httpx.Response(
                200, json={"data": [{"id": "q1", "name": "answer-quality-review"}]}
            )
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "existing",
                            "objectId": "trace-1",
                            "objectType": "TRACE",
                            "status": "PENDING",
                        }
                    ],
                    "meta": {"page": 1, "limit": 100, "totalItems": 1, "totalPages": 1},
                },
            )
        return httpx.Response(201, json={"id": "new", "status": "PENDING"})

    client = AnnotationQueueClient(BASE, "pk", "sk", transport=httpx.MockTransport(handler))
    result = client.enqueue(
        "answer-quality-review", business_trace_id="bt-1", object_id="trace-1", reason="low_score"
    )
    assert result.item_id == "existing"
    assert not any(method == "POST" for method, _ in calls)
    client.close()


def test_enqueue_uses_numeric_pages_from_public_pagination_meta() -> None:
    requested_pages: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("annotation-queues"):
            return httpx.Response(
                200, json={"data": [{"id": "q1", "name": "answer-quality-review"}]}
            )
        requested_pages.append(request.url.params.get("page"))
        page = request.url.params.get("page")
        rows = (
            []
            if page == "1"
            else [
                {
                    "id": "existing",
                    "objectId": "trace-1",
                    "objectType": "TRACE",
                    "status": "PENDING",
                }
            ]
        )
        return httpx.Response(
            200,
            json={
                "data": rows,
                "meta": {"page": int(page), "limit": 100, "totalItems": 1, "totalPages": 2},
            },
        )

    client = AnnotationQueueClient(BASE, "pk", "sk", transport=httpx.MockTransport(handler))
    result = client.enqueue(
        "answer-quality-review", business_trace_id="bt", object_id="trace-1", reason="failure"
    )
    assert result is not None and result.item_id == "existing"
    assert requested_pages == ["1", "2"]
    client.close()


def test_policy_covers_hard_triggers_and_stable_sampling() -> None:
    policy = ReviewPolicy(answer_threshold=0.7, selection_confidence_threshold=0.6, sample_rate=1.0)
    answer = policy.answer_reasons(
        business_trace_id="bt",
        scores={"answer_quality": 0.4},
        rule_judge_disagreement=False,
        failed=False,
        write_operation=False,
    )
    assert "low_score" in answer
    assert "rule_judge_disagreement" in policy.answer_reasons(
        business_trace_id="bt",
        scores={},
        rule_judge_disagreement=True,
        failed=False,
        write_operation=False,
    )
    assert "failure" in policy.answer_reasons(
        business_trace_id="bt",
        scores={},
        rule_judge_disagreement=False,
        failed=True,
        write_operation=False,
    )
    assert "write_operation" in policy.answer_reasons(
        business_trace_id="bt",
        scores={},
        rule_judge_disagreement=False,
        failed=False,
        write_operation=True,
    )
    assert "sampled" in policy.answer_reasons(
        business_trace_id="bt",
        scores={},
        rule_judge_disagreement=False,
        failed=False,
        write_operation=False,
    )
    assert policy.selection_reasons(business_trace_id="bt", confidence=0.2) == (
        "low_selection_confidence",
        "sampled",
    )


def test_enqueue_and_update_use_public_item_endpoints() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/public/annotation-queues":
            return httpx.Response(
                200, json={"data": [{"id": "q1", "name": "answer-quality-review"}]}
            )
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": [],
                    "meta": {"page": 1, "limit": 100, "totalItems": 0, "totalPages": 0},
                },
            )
        return httpx.Response(200, json={"id": "i1", "status": "COMPLETED"})

    client = AnnotationQueueClient(BASE, "pk", "sk", transport=httpx.MockTransport(handler))
    item = client.enqueue(
        "answer-quality-review",
        business_trace_id="bt",
        object_id="trace",
        reason="failure",
        force=True,
    )
    assert item is not None and item.item_id == "i1"
    updated = client.update_item("q1", "i1", status="COMPLETED")
    assert updated == item
    assert any(r.method == "POST" and r.url.path.endswith("/q1/items") for r in requests)
    assert requests[-1].method == "PATCH"
    assert requests[-1].url.path == "/api/public/annotation-queues/q1/items/i1"
    import json

    create_body = json.loads(next(r.content for r in requests if r.method == "POST"))
    assert create_body == {"objectId": "trace", "objectType": "TRACE"}
    assert json.loads(requests[-1].content) == {"status": "COMPLETED"}
    client.close()


def test_item_metadata_and_comment_payloads_are_rejected_by_public_schema() -> None:
    """The 4.16 strict schemas reject metadata/comment on create and update."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/public/annotation-queues":
            return httpx.Response(
                200, json={"data": [{"id": "q1", "name": "answer-quality-review"}]}
            )
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": [],
                    "meta": {"page": 1, "limit": 100, "totalItems": 0, "totalPages": 0},
                },
            )
        return httpx.Response(201, json={"id": "i1", "status": "PENDING"})

    client = AnnotationQueueClient(BASE, "pk", "sk", transport=httpx.MockTransport(handler))
    client.enqueue(
        "answer-quality-review",
        business_trace_id="bt",
        object_id="trace",
        reason="failure",
        force=True,
        metadata={"custom": "value"},
    )
    client.update_item("q1", "i1", status="COMPLETED", comment="reviewed")
    import json

    assert json.loads(next(r.content for r in requests if r.method == "POST")) == {
        "objectId": "trace",
        "objectType": "TRACE",
    }
    assert json.loads(requests[-1].content) == {"status": "COMPLETED"}
    client.close()


def test_retries_server_errors_but_not_client_errors() -> None:
    attempts = 0

    def retry_handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503 if attempts < 2 else 200, json={"data": []})

    client = AnnotationQueueClient(
        BASE, "pk", "sk", retry_attempts=3, transport=httpx.MockTransport(retry_handler)
    )
    assert client.list_queues() == []
    assert attempts == 2
    client.close()

    attempts = 0

    def bad_request_handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(400, json={"error": "bad request"})

    client = AnnotationQueueClient(
        BASE, "pk", "sk", retry_attempts=3, transport=httpx.MockTransport(bad_request_handler)
    )
    with pytest.raises(httpx.HTTPStatusError):
        client.list_queues()
    assert attempts == 1
    client.close()
