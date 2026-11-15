"""验收本机 Langfuse：默认运行合成探针，可显式运行真实 Agent/DeepSeek。"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT.parent / "backend"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(ROOT.parent))

DEFAULT_BASE_URL = "http://localhost:3000"
EXPECTED_LANGFUSE_VERSION = "4.16.0"
PROMPT_NAME = "bsagent-local-answer-v1"
DATASET_NAME = "bsagent-local-golden-v1"
EXPERIMENT_NAME = "bsagent-local-rule-evaluation-v1"
EVALUATOR_NAME = "bsagent-answer-quality-v1"
SCORE_VALUES: dict[str, bool | float | str] = {
    "task_success": True,
    "answer_quality": 0.9,
    "groundedness": 0.9,
    "selection_correctness": True,
    "citation_coverage": 1.0,
    "action_policy_compliance": True,
    "review_outcome": "accept",
}
logger = logging.getLogger("bs_agent.langfuse.smoke")


class SmokeBlocked(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--output", type=Path, default=ROOT / "smoke-result.json")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--debug-reasoning", action="store_true")
    modes.add_argument("--verify-human-annotation", action="store_true")
    modes.add_argument(
        "--real-agent",
        action="store_true",
        help="运行一条真实 Agent Graph 和 DeepSeek 请求；可能产生模型费用",
    )
    return parser


def validate_local_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("base_url must use a local Langfuse HTTP endpoint")
    return base_url.rstrip("/")


def _read_local_values(path: Path) -> dict[str, str]:
    """Read only key/value pairs from the private local platform dotenv file."""
    if not path.exists():
        return {}
    allowed = {
        "BS_AGENT_LANGFUSE_ENABLED",
        "BS_AGENT_LANGFUSE_BASE_URL",
        "BS_AGENT_LANGFUSE_PUBLIC_KEY",
        "BS_AGENT_LANGFUSE_SECRET_KEY",
        "BS_AGENT_LANGFUSE_CONTENT_MODE",
        "BS_AGENT_LANGFUSE_CAPTURE_REASONING",
        "BS_AGENT_DEEPSEEK_API_KEY",
        "LANGFUSE_INIT_PROJECT_PUBLIC_KEY",
        "LANGFUSE_INIT_PROJECT_SECRET_KEY",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "DEEPSEEK_API_KEY",
        "LANGFUSE_CONTENT_MODE",
        "LANGFUSE_CAPTURE_REASONING",
    }
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        name = name.strip()
        if name in allowed:
            values[name] = value.strip().strip("\"'")
    return values


def private_values_present(document: Any, private_values: list[str]) -> bool:
    """Find full private values in JSON-like data without short-ID false positives."""
    if isinstance(document, Mapping):
        return any(
            private_values_present(key, private_values)
            or private_values_present(value, private_values)
            for key, value in document.items()
        )
    if isinstance(document, (list, tuple)):
        return any(private_values_present(value, private_values) for value in document)
    if isinstance(document, str):
        return any(
            value and (value in document if len(value) >= 8 else value == document)
            for value in private_values
        )
    return False


def write_result(result: Mapping[str, Any], path: Path, *, private_values: list[str]) -> None:
    serialized = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if private_values_present(serialized, private_values):
        raise ValueError("sensitive_content_in_result")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialized, encoding="utf-8")


def _parse_io(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value


def run_trace(
    client: Any,
    business_trace_id: str,
    *,
    reasoning_canary: str,
    debug_reasoning: bool = False,
) -> dict[str, Any]:
    """Write a synthetic hierarchical trace; reasoning is opt-in and isolated."""
    trace_id = client.create_trace_id(seed=business_trace_id)
    observations: dict[str, str | None] = {}
    prompt = "Answer only from this synthetic evidence: {{chunk}}"
    answer = "Synthetic answer based on synthetic:chunk-1."
    with client.start_as_current_observation(
        trace_context={"trace_id": trace_id},
        name="bsagent.smoke-agent",
        as_type="agent",
        input={"question": "Synthetic local smoke question."},
        output={"status": "completed"},
        metadata={"business_trace_id": business_trace_id, "synthetic_only": True},
    ) as root:
        observations["agent"] = getattr(root, "id", None)
        with client.start_as_current_observation(
            name="bsagent.smoke-skill",
            as_type="span",
            input={"skill": "synthetic_answer"},
            output={"selected": True},
        ) as skill:
            observations["skill"] = getattr(skill, "id", None)
            with client.start_as_current_observation(
                name="bsagent.smoke-retriever",
                as_type="retriever",
                input={"query": "Synthetic local smoke question."},
                output={"chunks": [{"id": "synthetic:chunk-1", "text": "Synthetic evidence."}]},
            ) as retriever:
                observations["retriever"] = getattr(retriever, "id", None)
            with client.start_as_current_observation(
                name="bsagent.smoke-tool",
                as_type="tool",
                input={"tool": "synthetic_lookup", "arguments": {"key": "synthetic:chunk-1"}},
                output={"result": "Synthetic evidence."},
            ) as tool:
                observations["tool"] = getattr(tool, "id", None)
            generation_input: dict[str, Any] = {
                "prompt": prompt,
                "chunk": "Synthetic evidence.",
                "question": "Synthetic local smoke question.",
            }
            if debug_reasoning:
                generation_input["reasoning"] = reasoning_canary
            with client.start_as_current_observation(
                name="bsagent.smoke-generation",
                as_type="generation",
                input=generation_input,
                output={"answer": answer},
                model="deepseek-v4-flash",
                model_parameters={"temperature": 0},
                usage_details={"input": 18, "output": 8, "total": 26},
                cost_details={"input": 0.0000018, "output": 0.0000016, "total": 0.0000034},
            ) as generation:
                observations["generation"] = getattr(generation, "id", None)
    return {
        "business_trace_id": business_trace_id,
        "trace_id": trace_id,
        "observations": observations,
        "generation_source": "synthetic",
    }


def wait_trace(
    http: Any,
    trace_id: str,
    *,
    attempts: int = 30,
    delay: float = 1.0,
    expected_names: set[str] | None = None,
    minimum_count: int = 5,
) -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    params = {
        "traceId": trace_id,
        "fields": "core,basic,io,metadata,model,usage,metrics",
        "expandMetadata": "business_trace_id,synthetic_only",
        "limit": 100,
        "fromStartTime": (now - timedelta(minutes=10)).isoformat(),
        "toStartTime": (now + timedelta(minutes=5)).isoformat(),
    }
    for attempt in range(attempts):
        response = http.get("/api/public/v2/observations", params=params)
        response.raise_for_status()
        body = response.json()
        if body.get("meta", {}).get("cursor"):
            raise RuntimeError("smoke_observation_limit_exceeded")
        rows = body.get("data", [])
        names = {row.get("name") for row in rows}
        if len(rows) >= minimum_count and (expected_names is None or expected_names <= names):
            return rows
        if attempt + 1 < attempts:
            time.sleep(delay)
    raise RuntimeError("smoke_observation_ingestion_timeout")


def _observed_server_version(health_payload: Any) -> str | None:
    if not isinstance(health_payload, Mapping):
        return None
    version = health_payload.get("version")
    return version if isinstance(version, str) and version else None


def _check_trace(
    rows: list[dict[str, Any]], trace: Mapping[str, Any], *, reasoning_canary: str, debug: bool
) -> dict[str, bool | int | float]:
    by_name = {item.get("name"): item for item in rows}
    expected = {
        "bsagent.smoke-agent": "AGENT",
        "bsagent.smoke-skill": "SPAN",
        "bsagent.smoke-retriever": "RETRIEVER",
        "bsagent.smoke-tool": "TOOL",
        "bsagent.smoke-generation": "GENERATION",
    }
    root = by_name.get("bsagent.smoke-agent", {})
    parent_chain = all(
        by_name.get(name, {}).get("parentObservationId") == by_name.get(parent, {}).get("id")
        for name, parent in (
            ("bsagent.smoke-skill", "bsagent.smoke-agent"),
            ("bsagent.smoke-retriever", "bsagent.smoke-skill"),
            ("bsagent.smoke-tool", "bsagent.smoke-skill"),
            ("bsagent.smoke-generation", "bsagent.smoke-skill"),
        )
    )
    metadata = root.get("metadata") if isinstance(root.get("metadata"), Mapping) else {}
    expected_generation_input = {
        "prompt": "Answer only from this synthetic evidence: {{chunk}}",
        "chunk": "Synthetic evidence.",
        "question": "Synthetic local smoke question.",
    }
    if debug:
        expected_generation_input["reasoning"] = reasoning_canary
    content_ok = (
        _parse_io(by_name.get("bsagent.smoke-agent", {}).get("input"))
        == {"question": "Synthetic local smoke question."}
        and _parse_io(by_name.get("bsagent.smoke-agent", {}).get("output"))
        == {"status": "completed"}
        and _parse_io(by_name.get("bsagent.smoke-skill", {}).get("input"))
        == {"skill": "synthetic_answer"}
        and _parse_io(by_name.get("bsagent.smoke-skill", {}).get("output")) == {"selected": True}
        and _parse_io(by_name.get("bsagent.smoke-retriever", {}).get("output"))
        == {"chunks": [{"id": "synthetic:chunk-1", "text": "Synthetic evidence."}]}
        and _parse_io(by_name.get("bsagent.smoke-tool", {}).get("input"))
        == {"tool": "synthetic_lookup", "arguments": {"key": "synthetic:chunk-1"}}
        and _parse_io(by_name.get("bsagent.smoke-tool", {}).get("output"))
        == {"result": "Synthetic evidence."}
        and _parse_io(by_name.get("bsagent.smoke-generation", {}).get("output"))
        == {"answer": "Synthetic answer based on synthetic:chunk-1."}
        and _parse_io(by_name.get("bsagent.smoke-generation", {}).get("input"))
        == expected_generation_input
    )
    generation = by_name.get("bsagent.smoke-generation", {})
    reasoning_seen = private_values_present(rows, [reasoning_canary])
    usage = generation.get("usageDetails") or {}
    costs = generation.get("costDetails") or {}
    return {
        "observation_count": len(rows),
        "hierarchy_correct": parent_chain
        and all(by_name.get(name, {}).get("type") == kind for name, kind in expected.items()),
        "business_trace_id_matches": metadata.get("business_trace_id")
        == trace["business_trace_id"],
        "synthetic_content_readback": content_ok,
        "reasoning_privacy_correct": reasoning_seen is debug,
        "deepseek_usage_fields_present": generation.get("providedModelName") == "deepseek-v4-flash"
        and usage.get("input") == 18
        and usage.get("output") == 8
        and usage.get("total") == 26,
        "deepseek_cost_fields_present": isinstance(costs, Mapping)
        and isinstance(costs.get("input"), (int, float))
        and isinstance(costs.get("output"), (int, float))
        and isinstance(generation.get("totalCost"), (int, float)),
        "generation_source_synthetic": trace.get("generation_source") == "synthetic",
    }


def _is_descendant(
    observation: Mapping[str, Any],
    ancestor_id: str,
    by_id: Mapping[str, Mapping[str, Any]],
) -> bool:
    parent_id = observation.get("parentObservationId")
    visited: set[str] = set()
    while isinstance(parent_id, str) and parent_id and parent_id not in visited:
        if parent_id == ancestor_id:
            return True
        visited.add(parent_id)
        parent = by_id.get(parent_id)
        parent_id = parent.get("parentObservationId") if parent else None
    return False


def _positive_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _generation_has_cost(generation: Mapping[str, Any]) -> bool:
    details = generation.get("costDetails")
    candidates = [
        generation.get("totalCost"),
        generation.get("calculatedTotalCost"),
        generation.get("providedTotalCost"),
    ]
    if isinstance(details, Mapping):
        candidates.extend(details.get(key) for key in ("input", "output", "total"))
    return any(_positive_number(value) for value in candidates)


def _check_real_trace(
    rows: list[dict[str, Any]],
    trace: Mapping[str, Any],
    *,
    private_values: list[str],
) -> dict[str, bool | int]:
    roots = [row for row in rows if row.get("name") == "bs-agent.run"]
    root = roots[0] if len(roots) == 1 else {}
    root_id = root.get("id")
    by_id = {row["id"]: row for row in rows if isinstance(row.get("id"), str)}
    deepseek_generations = [
        row
        for row in rows
        if row.get("type") == "GENERATION"
        and (
            row.get("name") == "deepseek.chat.completions"
            or "deepseek" in str(row.get("providedModelName") or row.get("model") or "").lower()
        )
    ]
    managed = [
        row for row in rows if row.get("name") in {"smoke.rag.retrieve", "smoke.tool.readonly"}
    ]
    managed_names = {row.get("name") for row in managed}
    descendants = [*deepseek_generations, *managed]
    metadata = root.get("metadata") if isinstance(root.get("metadata"), Mapping) else {}
    usage_present = any(
        isinstance((usage := row.get("usageDetails")), Mapping)
        and _positive_number(usage.get("input"))
        and _positive_number(usage.get("output"))
        and _positive_number(usage.get("total"))
        for row in deepseek_generations
    )
    content = trace.get("content") if isinstance(trace.get("content"), Mapping) else {}
    mode = trace.get("content_mode")
    capture_reasoning = trace.get("capture_reasoning") is True
    reasoning = content.get("reasoning")
    reasoning_available = isinstance(reasoning, str) and bool(reasoning)
    reasoning_seen = reasoning_available and private_values_present(rows, [reasoning])
    expected_reasoning_seen = mode == "local_debug" and capture_reasoning and reasoning_available
    return {
        "observation_count": len(rows),
        "single_agent_root": len(roots) == 1 and root.get("type") == "AGENT",
        "business_trace_id_matches": metadata.get("business_trace_id")
        == trace.get("business_trace_id"),
        "agent_run_completed": trace.get("business_completed") is True,
        "managed_observations_are_descendants": bool(root_id)
        and managed_names == {"smoke.rag.retrieve", "smoke.tool.readonly"}
        and bool(deepseek_generations)
        and all(_is_descendant(row, str(root_id), by_id) for row in descendants),
        "deepseek_generation_present": bool(deepseek_generations),
        "deepseek_provider_usage_present": usage_present,
        "deepseek_cost_present": any(_generation_has_cost(row) for row in deepseek_generations),
        "prompt_uploaded_per_policy": isinstance(content.get("prompt"), str)
        and private_values_present(rows, [content["prompt"]]),
        "answer_uploaded_per_policy": isinstance(content.get("answer"), str)
        and private_values_present(rows, [content["answer"]]),
        "rag_content_uploaded_per_policy": isinstance(content.get("rag"), str)
        and private_values_present(rows, [content["rag"]]),
        "tool_content_uploaded_per_policy": isinstance(content.get("tool"), str)
        and private_values_present(rows, [content["tool"]]),
        "reasoning_policy_correct": reasoning_seen is expected_reasoning_seen,
        "reasoning_returned_by_provider": reasoning_available,
        "credentials_absent": not private_values_present(rows, private_values),
    }


def _run_real_agent(settings: Any, business_trace_id: str) -> tuple[dict[str, Any], list[str]]:
    """Run the real Graph/DeepSeek path with controlled local RAG/tool content."""
    from app.graph.runtime import run_question_answer_graph
    from app.intent.catalog import BusinessIntentCatalog
    from app.intent.schemas import BusinessIntentProfile
    from app.llm.deepseek import DeepSeekClient
    from app.operations.langfuse_observability import (
        configure_langfuse,
        langfuse_observation,
        update_langfuse_observation,
    )
    from app.tools.registry import ToolRegistry

    unique = uuid4().hex
    prompt_canary = f"real-prompt-canary-{unique}"
    rag_canary = f"real-rag-canary-{unique}"
    tool_canary = f"real-tool-canary-{unique}"
    question = (
        "执行本地 Langfuse 验收：只使用本地只读工具取得合成证据，"
        "然后用一句中文回答；不要调用外部检索。"
    )
    llm = DeepSeekClient(
        api_key=settings.deepseek_api_key,
        model=settings.deepseek_model,
        base_url=settings.deepseek_base_url,
        timeout_seconds=settings.llm_timeout_seconds,
    )
    captured: dict[str, str] = {}

    def tool_node(state: Mapping[str, Any]) -> dict[str, Any]:
        with langfuse_observation(
            as_type="retriever",
            name="smoke.rag.retrieve",
            input={"query": "local controlled smoke evidence"},
        ) as retriever:
            update_langfuse_observation(
                retriever,
                output={"chunks": [{"id": "smoke:1", "content": rag_canary}]},
            )
        with langfuse_observation(
            as_type="tool",
            name="smoke.tool.readonly",
            input={"operation": "read_local_smoke_evidence"},
        ) as tool:
            result = asyncio.run(
                llm.complete(
                    system_prompt=(
                        f"{prompt_canary}。你正在做只读本地验收。"
                        "用一句中文复述用户给出的合成证据，不要补充外部事实。"
                    ),
                    user_prompt=f"合成证据：{rag_canary}；工具标记：{tool_canary}",
                    temperature=0,
                    max_tokens=96,
                    thinking=True,
                )
            )
            captured["answer"] = result.content
            captured["reasoning"] = result.reasoning_content
            update_langfuse_observation(
                tool,
                output={"marker": tool_canary, "answer": result.content},
            )
        active_task = state.get("active_task") or {}
        return {
            "task_results": [
                {
                    "task_id": active_task.get("id", "smoke_read"),
                    "status": "success",
                    "content": result.content,
                    "tool_name": "smoke.tool.readonly",
                }
            ]
        }

    config = settings.model_copy(
        update={
            "langfuse_enabled": True,
            "langfuse_sample_rate": 1.0,
        }
    )
    runtime = configure_langfuse(config=config)
    if runtime is None:
        raise RuntimeError("real_langfuse_runtime_unavailable")
    try:
        catalog = BusinessIntentCatalog(
            [
                BusinessIntentProfile(
                    id="smoke.local_readonly",
                    intent_type="local_smoke",
                    domain="observability",
                    subdomain="readonly_validation",
                    trigger_keywords=["Langfuse", "验收", "合成证据"],
                    trigger_examples=[question],
                    description="只读本地观测验收，不获取外部事实。",
                )
            ]
        )
        result = run_question_answer_graph(
            business_intent_catalog=catalog,
            session_id=f"smoke-session-{unique}",
            session_key=f"smoke-session-{unique}",
            tenant_id="smoke-local-tenant",
            user_id="smoke-local-user",
            user_message=question,
            request_id=f"smoke-request-{unique}",
            trace_id=business_trace_id,
            skill_agent_node=lambda state: state,
            tool_agent_node=tool_node,
            allow_test_tool_agent_override=True,
            tool_registry_factory=lambda: ToolRegistry().snapshot(),
            llm_client=llm,
        )
        runtime.client.flush()
        final_response = result.get("final_response") or {}
        if "answer" not in captured:
            raise RuntimeError("controlled_smoke_tool_not_executed")
        captured.setdefault("answer", str(final_response.get("content") or ""))
        captured.setdefault("reasoning", "")
        trace_id = runtime.client.create_trace_id(seed=business_trace_id)
        trace = {
            "business_trace_id": business_trace_id,
            "trace_id": trace_id,
            "generation_source": "real_deepseek",
            "content_mode": settings.langfuse_content_mode,
            "capture_reasoning": settings.langfuse_capture_reasoning,
            "business_completed": bool(final_response) and not bool(result.get("errors")),
            "content": {
                "prompt": prompt_canary,
                "answer": captured["answer"],
                "rag": rag_canary,
                "tool": tool_canary,
                "reasoning": captured["reasoning"],
            },
        }
        private_content = [
            value
            for value in (
                prompt_canary,
                captured["answer"],
                rag_canary,
                tool_canary,
                captured["reasoning"],
            )
            if value
        ]
        return trace, private_content
    finally:
        runtime.shutdown()


def _api_data(payload: Any) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, Mapping) else getattr(payload, "data", None)
    if data is not None:
        return [row for row in data if isinstance(row, dict) or hasattr(row, "name")]
    return []


def _read_scores(http: Any, trace_id: str) -> list[dict[str, Any]]:
    response = http.get(
        "/api/public/v3/scores",
        params={"traceId": trace_id, "fields": "details,subject,annotation", "limit": 100},
    )
    response.raise_for_status()
    return _api_data(response.json())


def _read_queue_items(http: Any, queue_id: str) -> list[dict[str, Any]]:
    response = http.get(
        f"/api/public/annotation-queues/{queue_id}/items", params={"page": 1, "limit": 100}
    )
    response.raise_for_status()
    return _api_data(response.json())


def _read_queues(http: Any) -> list[dict[str, Any]]:
    response = http.get("/api/public/annotation-queues", params={"page": 1, "limit": 100})
    response.raise_for_status()
    return _api_data(response.json())


def _read_prompt_dataset_experiment(
    http: Any, *, dataset_name: str, experiment_name: str
) -> dict[str, bool]:
    prompt = http.get(f"/api/public/v2/prompts/{PROMPT_NAME}", params={"label": "production"})
    dataset = http.get(f"/api/public/datasets/{dataset_name}")
    now = datetime.now(UTC).isoformat()
    experiment = http.get(
        "/api/public/experiments",
        params={
            "name": experiment_name,
            "fromStartTime": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
            "toStartTime": now,
            "limit": 100,
        },
    )
    prompt.raise_for_status()
    dataset.raise_for_status()
    experiment.raise_for_status()
    return {
        "prompt_readback": bool(prompt.json().get("data") or prompt.json().get("name")),
        "dataset_readback": bool(dataset.json().get("data") or dataset.json().get("name")),
        "experiment_readback": any(
            (row.get("name") if isinstance(row, Mapping) else getattr(row, "name", None))
            == experiment_name
            for row in _api_data(experiment.json())
        ),
    }


def _ensure_evaluators(client: Any, *, deepseek_key: str, settings: Any) -> dict[str, bool]:
    from app.operations.langfuse_evaluation import (
        EVALUATOR_NAME,
        RuleEvaluationInput,
        ensure_native_answer_evaluator,
        evaluate_rules,
    )

    api = getattr(client, "api", client)
    if deepseek_key:
        ensure_native_answer_evaluator(
            client, deepseek_key, settings.deepseek_base_url, settings.deepseek_model
        )
    evaluator_api = getattr(client, "evaluators", None) or api.evaluators
    rules_api = getattr(client, "evaluation_rules", None) or api.evaluation_rules
    evaluators = _api_data(evaluator_api.list(limit=100))
    rules = _api_data(rules_api.list(limit=100))
    rule_result = evaluate_rules(RuleEvaluationInput(completed=True))
    return {
        "rule_evaluator_confirmed": rule_result.scores.get("task_success") is True,
        "native_evaluator_confirmed": any(
            getattr(item, "name", None) == EVALUATOR_NAME for item in evaluators
        ),
        "evaluation_rule_confirmed": any(
            getattr(item, "name", None) == EVALUATOR_NAME for item in rules
        ),
    }


def _verify_human_annotation(http: Any, previous: Mapping[str, Any]) -> dict[str, Any]:
    trace = previous.get("trace") if isinstance(previous.get("trace"), Mapping) else {}
    queue_ids = previous.get("queue_ids") if isinstance(previous.get("queue_ids"), Mapping) else {}
    trace_id = trace.get("trace_id")
    items = previous.get("queue_items") if isinstance(previous.get("queue_items"), Mapping) else {}
    limitations: list[str] = []
    checks: dict[str, bool] = {}
    for queue_name, required_scores in (
        (
            "answer-quality-review",
            {"answer_quality", "groundedness", "action_policy_compliance", "review_outcome"},
        ),
        ("skill-tool-selection-review", {"selection_correctness"}),
    ):
        queue_id = queue_ids.get(queue_name)
        expected_item_id = items.get(queue_name)
        if not queue_id or not expected_item_id:
            raise ValueError("human_annotation_reference_missing")
        queue_items = _read_queue_items(http, str(queue_id))
        item = next((row for row in queue_items if row.get("id") == expected_item_id), None)
        checks[f"{queue_name}_completed"] = bool(item and item.get("status") == "COMPLETED")
        score_response = http.get(
            "/api/public/v3/scores",
            params={
                "traceId": trace_id,
                "queueId": queue_id,
                "fields": "details,subject,annotation",
                "limit": 100,
            },
        )
        score_response.raise_for_status()
        scores = _api_data(score_response.json())
        human_scores = {
            score.get("name")
            for score in scores
            if score.get("source") == "ANNOTATION" and score.get("queueId") == queue_id
        }
        checks[f"{queue_name}_human_scores_present"] = required_scores <= human_scores
        comment_field_available = any("comment" in score for score in scores)
        comment_scores = [score for score in scores if score.get("comment")]
        if comment_field_available:
            checks[f"{queue_name}_comment_readable"] = bool(comment_scores)
        else:
            limitations.append(f"{queue_name}_comment_not_exposed_by_public_api")
    return {
        "trace_id": trace_id,
        "checks": checks,
        "limitations": limitations,
        "passed": all(checks.values()),
    }


def merge_human_annotation_result(
    previous: Mapping[str, Any], verification: Mapping[str, Any]
) -> dict[str, Any]:
    """Keep the original smoke IDs available for repeatable annotation checks."""
    result = dict(previous)
    result["human_annotation_verification"] = dict(verification)
    result["checks"] = {
        **(previous.get("checks", {}) if isinstance(previous.get("checks"), Mapping) else {}),
        **(
            verification.get("checks", {})
            if isinstance(verification.get("checks"), Mapping)
            else {}
        ),
    }
    result["limitations"] = list(previous.get("limitations", [])) + list(
        verification.get("limitations", [])
    )
    result["passed"] = bool(previous.get("passed")) and bool(verification.get("passed"))
    return result


def _load_runtime(
    base_url: str, *, create_client: bool = True
) -> tuple[Any | None, Any, Any, dict[str, str], list[str]]:
    import httpx
    from app.core.config import Settings

    from langfuse import Langfuse

    project_values = _read_local_values(ROOT.parent / ".env.local")
    platform_values = _read_local_values(ROOT / ".env")
    configured = Settings(_env_file=ROOT.parent / ".env.local")
    public_key = (
        os.environ.get("BS_AGENT_LANGFUSE_PUBLIC_KEY")
        or os.environ.get("LANGFUSE_PUBLIC_KEY")
        or configured.langfuse_public_key
        or project_values.get("BS_AGENT_LANGFUSE_PUBLIC_KEY", "")
        or platform_values.get("LANGFUSE_INIT_PROJECT_PUBLIC_KEY", "")
    )
    secret_key = (
        os.environ.get("BS_AGENT_LANGFUSE_SECRET_KEY")
        or os.environ.get("LANGFUSE_SECRET_KEY")
        or configured.langfuse_secret_key
        or project_values.get("BS_AGENT_LANGFUSE_SECRET_KEY", "")
        or platform_values.get("LANGFUSE_INIT_PROJECT_SECRET_KEY", "")
    )
    deepseek_key = (
        os.environ.get("BS_AGENT_DEEPSEEK_API_KEY")
        or os.environ.get("DEEPSEEK_API_KEY")
        or configured.deepseek_api_key
        or project_values.get("BS_AGENT_DEEPSEEK_API_KEY", "")
        or platform_values.get("DEEPSEEK_API_KEY", "")
    )
    if not public_key or not secret_key:
        raise RuntimeError("local_langfuse_credentials_missing")
    settings = Settings(
        _env_file=ROOT.parent / ".env.local",
        langfuse_enabled=True,
        langfuse_base_url=base_url,
        langfuse_public_key=public_key,
        langfuse_secret_key=secret_key,
        deepseek_api_key=deepseek_key,
    )
    client = None
    if create_client:
        client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            base_url=base_url,
            environment="local",
            mask_otel_spans=None,
        )
    http = httpx.Client(base_url=base_url, auth=(public_key, secret_key), timeout=10)
    private_values = [value for value in (public_key, secret_key, deepseek_key) if value]
    return (
        client,
        http,
        settings,
        {"public_key": public_key, "secret_key": secret_key, "deepseek_key": deepseek_key},
        private_values,
    )


def _execute_real(
    http: Any,
    settings: Any,
    *,
    private_values: list[str],
) -> tuple[dict[str, Any], list[str]]:
    if not settings.deepseek_api_key:
        raise SmokeBlocked("real_deepseek_credentials_missing")
    try:
        health = http.get("/api/public/health")
        health.raise_for_status()
    except Exception as exc:
        raise SmokeBlocked("local_langfuse_service_unavailable") from exc
    observed_version = _observed_server_version(health.json())
    business_trace_id = f"real-smoke-{uuid4().hex}"
    try:
        trace, private_content = _run_real_agent(settings, business_trace_id)
    except Exception as exc:
        raise SmokeBlocked("real_agent_or_deepseek_execution_failed") from exc
    try:
        rows = wait_trace(
            http,
            trace["trace_id"],
            expected_names={"bs-agent.run", "smoke.rag.retrieve", "smoke.tool.readonly"},
            minimum_count=4,
        )
    except Exception as exc:
        raise SmokeBlocked("local_langfuse_ingestion_or_readback_failed") from exc
    checks = _check_real_trace(rows, trace, private_values=private_values)
    checks["server_version_matches"] = observed_version == EXPECTED_LANGFUSE_VERSION
    required = {
        name: value
        for name, value in checks.items()
        if name not in {"observation_count", "reasoning_returned_by_provider"}
    }
    deepseek_rows = [
        row
        for row in rows
        if row.get("type") == "GENERATION"
        and (
            row.get("name") == "deepseek.chat.completions"
            or "deepseek" in str(row.get("providedModelName") or row.get("model") or "").lower()
        )
    ]
    result = {
        "acceptance_mode": "real_agent",
        "server_version_observed": observed_version,
        "server_version_expected": EXPECTED_LANGFUSE_VERSION,
        "base_url": settings.langfuse_base_url,
        "trace": {
            "business_trace_id": trace["business_trace_id"],
            "trace_id": trace["trace_id"],
            "generation_source": "real_deepseek",
            "content_mode": trace["content_mode"],
            "capture_reasoning": trace["capture_reasoning"],
        },
        "deepseek_generations": [
            {
                "id": row.get("id"),
                "name": row.get("name"),
                "model": row.get("providedModelName") or row.get("model"),
                "usage": row.get("usageDetails") or {},
                "cost_available": _generation_has_cost(row),
            }
            for row in deepseek_rows
        ],
        "checks": checks,
        "passed": all(bool(value) for value in required.values()),
        "limitations": [
            "single_local_agent_run_only",
            "rag_and_tool_use_controlled_local_smoke_evidence",
        ],
    }
    return result, private_content


def _execute(
    client: Any,
    http: Any,
    credentials: Mapping[str, str],
    *,
    base_url: str,
    debug_reasoning: bool,
    reasoning_canary: str,
) -> dict[str, Any]:
    from app.core.config import Settings
    from app.operations.langfuse_annotation import AnnotationQueueClient
    from app.operations.langfuse_scores import ensure_score_configs, record_langfuse_score

    from scripts import langfuse_examples as examples

    health = http.get("/api/public/health")
    health.raise_for_status()
    observed_version = _observed_server_version(health.json())
    business_trace_id = f"smoke-{uuid4().hex}"
    trace = run_trace(
        client,
        business_trace_id,
        reasoning_canary=reasoning_canary,
    )
    client.flush()
    rows = wait_trace(http, trace["trace_id"])
    trace_checks = _check_trace(rows, trace, reasoning_canary=reasoning_canary, debug=False)

    debug_result = None
    if debug_reasoning:
        debug_business_id = f"smoke-debug-{uuid4().hex}"
        debug_trace = run_trace(
            client,
            debug_business_id,
            reasoning_canary=reasoning_canary,
            debug_reasoning=True,
        )
        client.flush()
        debug_rows = wait_trace(http, debug_trace["trace_id"])
        debug_result = _check_trace(
            debug_rows, debug_trace, reasoning_canary=reasoning_canary, debug=True
        )

    config_ids = ensure_score_configs(client)
    if set(config_ids) != set(SCORE_VALUES):
        raise RuntimeError("smoke_score_configs_incomplete")
    for name, value in SCORE_VALUES.items():
        record_langfuse_score(
            client,
            name=name,
            value=value,
            config_id=config_ids[name],
            trace_id=trace["trace_id"],
            observation_id=trace["observations"]["tool"]
            if name == "selection_correctness"
            else None,
            evaluator="smoke-synthetic-rules",
            evaluator_version="1",
            rubric_version="bsagent-judge-v1",
            run_id=business_trace_id,
            dataset_version="bsagent-synthetic-v1",
            environment="local",
        )
    client.flush()
    scores = _read_scores(http, trace["trace_id"])
    score_checks = {
        name: any(score.get("name") == name for score in scores) for name in SCORE_VALUES
    }

    settings = Settings(
        _env_file=None,
        langfuse_enabled=True,
        langfuse_base_url=base_url,
        langfuse_public_key=credentials["public_key"],
        langfuse_secret_key=credentials["secret_key"],
        deepseek_api_key=credentials.get("deepseek_key", ""),
    )
    evaluator_checks = _ensure_evaluators(
        client, deepseek_key=credentials.get("deepseek_key", ""), settings=settings
    )

    with AnnotationQueueClient(
        base_url,
        credentials["public_key"],
        credentials["secret_key"],
        score_config_ids=config_ids,
    ) as queue_client:
        queue_refs = queue_client.ensure_queues(score_config_ids=config_ids)
        queue_by_name = {queue.name: queue for queue in queue_refs}
        answer_item = queue_client.enqueue(
            "answer-quality-review",
            business_trace_id=business_trace_id,
            object_id=trace["trace_id"],
            reason="smoke_synthetic",
            force=True,
        )
        selection_item = queue_client.enqueue(
            "skill-tool-selection-review",
            business_trace_id=business_trace_id,
            object_id=trace["observations"]["tool"] or "",
            object_type="OBSERVATION",
            reason="smoke_synthetic",
            force=True,
        )
    client.flush()
    queue_items = {
        "answer-quality-review": answer_item.item_id,
        "skill-tool-selection-review": selection_item.item_id,
    }
    queue_checks: dict[str, bool] = {}
    for queue_name, item_id in queue_items.items():
        queue_id = queue_by_name[queue_name].queue_id
        found = next(
            (row for row in _read_queue_items(http, queue_id) if row.get("id") == item_id), None
        )
        queue_checks[queue_name] = bool(found and found.get("status") == "PENDING")

    examples.run_prompt(client)
    prompt = client.get_prompt(PROMPT_NAME, label="production", type="text")
    examples.run_dataset(client)
    dataset = client.get_dataset(DATASET_NAME)
    experiment_result = examples.run_experiment(client)
    client.flush()
    artifact_checks = _read_prompt_dataset_experiment(
        http, dataset_name=DATASET_NAME, experiment_name=experiment_result["experiment"]
    )
    artifact_checks["prompt_version_present"] = bool(getattr(prompt, "version", None))
    artifact_checks["dataset_items_present"] = bool(getattr(dataset, "items", None))

    passed_checks: dict[str, bool | int | float] = dict(trace_checks)
    passed_checks["server_version_matches"] = observed_version == EXPECTED_LANGFUSE_VERSION
    passed_checks.update({f"score_{name}_readback": check for name, check in score_checks.items()})
    passed_checks.update(evaluator_checks)
    passed_checks.update({f"queue_{name}_pending": check for name, check in queue_checks.items()})
    passed_checks.update(artifact_checks)
    if debug_result is not None:
        passed_checks.update({f"debug_{name}": value for name, value in debug_result.items()})

    observation_summary = [
        {
            "id": row.get("id"),
            "name": row.get("name"),
            "type": row.get("type"),
            "parent_observation_id": row.get("parentObservationId"),
            "usage_input": (row.get("usageDetails") or {}).get("input"),
            "usage_output": (row.get("usageDetails") or {}).get("output"),
            "usage_total": (row.get("usageDetails") or {}).get("total"),
            "cost_input": (row.get("costDetails") or {}).get("input"),
            "cost_output": (row.get("costDetails") or {}).get("output"),
            "cost_total": row.get("totalCost"),
        }
        for row in rows
    ]
    return {
        "acceptance_mode": "synthetic_platform_probe",
        "server_version_observed": observed_version,
        "server_version_expected": EXPECTED_LANGFUSE_VERSION,
        "base_url": base_url,
        "trace": trace,
        "observations": observation_summary,
        "scores": [{"name": name, "value": value} for name, value in SCORE_VALUES.items()],
        "queue_ids": {name: queue_by_name[name].queue_id for name in queue_items},
        "queue_items": queue_items,
        "prompt_id": getattr(prompt, "id", None),
        "dataset_id": getattr(dataset, "id", None),
        "experiment_name": experiment_result["experiment"],
        "checks": passed_checks,
        "passed": all(bool(value) for value in passed_checks.values()),
        "generation_source": "synthetic",
        "limitations": ["deepseek_usage_and_cost_are_synthetic"],
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    private_values: list[str] = []
    try:
        base_url = validate_local_url(args.base_url)
        client, http, settings, credentials, private_values = _load_runtime(
            base_url,
            create_client=not args.real_agent,
        )
    except Exception:  # noqa: BLE001 - 配置异常可能携带密钥，统一输出安全阻塞码
        failure = {
            "passed": False,
            "blocked": True,
            "blocker": "local_configuration_or_credentials_unavailable",
        }
        print(json.dumps(failure, ensure_ascii=False, sort_keys=True))
        return 2
    private_values.append(f"authorization-canary-{uuid4().hex}")
    reasoning_canary = f"synthetic-reasoning-canary-{uuid4().hex}"
    private_values.append(reasoning_canary)
    try:
        if args.verify_human_annotation:
            previous = json.loads(args.output.read_text(encoding="utf-8"))
            verification = _verify_human_annotation(http, previous)
            result = merge_human_annotation_result(previous, verification)
        elif args.real_agent:
            result, real_private_values = _execute_real(
                http,
                settings,
                private_values=private_values,
            )
            private_values.extend(real_private_values)
        else:
            if args.debug_reasoning and (
                settings.langfuse_content_mode != "local_debug"
                or not settings.langfuse_capture_reasoning
            ):
                raise ValueError("debug_reasoning_requires_local_debug_and_capture_opt_in")
            result = _execute(
                client,
                http,
                credentials,
                base_url=base_url,
                debug_reasoning=args.debug_reasoning,
                reasoning_canary=reasoning_canary,
            )
        write_result(result, args.output, private_values=private_values)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("passed") else 1
    except SmokeBlocked as exc:
        result = {
            "acceptance_mode": "real_agent" if args.real_agent else "synthetic_platform_probe",
            "passed": False,
            "blocked": True,
            "blocker": exc.code,
        }
        write_result(result, args.output, private_values=private_values)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 2
    except Exception:  # noqa: BLE001 - SDK 异常可能携带请求正文或凭证
        # SDK/API exception bodies can contain request headers, payloads, or local credentials.
        result = {
            "acceptance_mode": "real_agent" if args.real_agent else "synthetic_platform_probe",
            "passed": False,
            "error": "local_smoke_failed",
        }
        write_result(result, args.output, private_values=private_values)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 1
    finally:
        http.close()
        if client is not None:
            try:
                client.shutdown()
            except Exception:  # noqa: BLE001 - shutdown 失败不能覆盖 smoke 结果
                logger.warning("langfuse_smoke_shutdown_failed")


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    raise SystemExit(main())
