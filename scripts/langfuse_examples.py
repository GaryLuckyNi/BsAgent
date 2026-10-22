"""以合成数据演示 Langfuse trace、评分、prompt、标注、Dataset 和实验。"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.operations.langfuse_annotation import AnnotationQueueClient
from app.operations.langfuse_evaluation import (
    DeepSeekJudge,
    RuleEvaluationInput,
    ensure_native_answer_evaluator,
    evaluate_rules,
)
from app.operations.langfuse_observability import hash_langfuse_identifier
from app.operations.langfuse_scores import SCORE_SPECS, ensure_score_configs

DATASET_NAME = "bsagent-local-golden-v1"
PROMPT_NAME = "bsagent-local-answer-v1"
DATASET_VERSION = "bsagent-synthetic-v1"
GRAPH_VERSION = "agent-graph-local-v1"
SKILL_VERSION = "skill-catalog-local-v1"
KNOWLEDGE_VERSION = "knowledge-snapshot-synthetic-v1"
_DATASET_PATH = ROOT / "langfuse" / "dataset-items.json"


def _synthetic_items() -> list[dict[str, Any]]:
    data = json.loads(_DATASET_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, list) or len(data) != 3:
        raise ValueError("synthetic dataset must contain exactly three examples")
    return data


def _identity_digest(value: str) -> str:
    return hash_langfuse_identifier(value)


def _dataset_item_id(example_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"bsagent:{DATASET_NAME}:{example_id}"))


def run_trace(client: Any) -> dict[str, Any]:
    """建立一条带 retriever、tool 和 generation 的合成 trace。"""
    trace_id = client.create_trace_id(seed="bsagent-langfuse-examples-trace-v1")
    observations: dict[str, Any] = {}
    context = {
        "trace_id": trace_id,
    }
    user_hash = _identity_digest("synthetic-demo-user")
    session_hash = _identity_digest("synthetic-demo-session")
    try:
        from langfuse import propagate_attributes

        attributes = propagate_attributes(user_id=user_hash, session_id=session_hash)
    except ImportError:
        attributes = nullcontext()
    with (
        attributes,
        client.start_as_current_observation(
            trace_context=context,
            name="bsagent.example-agent",
            as_type="agent",
            input={"question": "Synthetic question: what is the current release?"},
            output={"status": "completed"},
            metadata={
                "dataset_version": DATASET_VERSION,
                "user_hash": user_hash,
                "session_hash": session_hash,
            },
        ) as root,
    ):
        observations["trace_id"] = trace_id
        with client.start_as_current_observation(
            name="bsagent.example-retriever",
            as_type="retriever",
            input={"query": "Synthetic release status"},
            output={"document_ids": ["synthetic:doc-1"]},
        ):
            pass
        with client.start_as_current_observation(
            name="bsagent.example-tool",
            as_type="tool",
            input={"tool": "rag_search", "query": "Synthetic release status"},
            output={"result": "Synthetic release 2.4"},
        ) as tool_observation:
            observations["selection_observation_id"] = getattr(tool_observation, "id", None)
        prompt = "Use only this synthetic evidence: {{context}}. Answer: {{question}}"
        with client.start_as_current_observation(
            name="bsagent.example-answer",
            as_type="generation",
            input={
                "prompt": prompt,
                "context": "Synthetic release 2.4",
                "question": "Current release?",
            },
            output={"answer": "Synthetic release 2.4 is current. [synthetic:doc-1]"},
            model="synthetic-local-model",
            model_parameters={"temperature": 0},
        ):
            pass
    observations["root_observation_id"] = getattr(root, "id", None)
    return observations


def run_score(client: Any, trace_id: str | None = None) -> dict[str, Any]:
    """写入 boolean、numeric、categorical 三种稳定 Score。"""
    trace_id = trace_id or run_trace(client)["trace_id"]
    config_ids = ensure_score_configs(client)
    required = ("task_success", "answer_quality", "review_outcome")
    if any(not config_ids.get(name) for name in required):
        raise RuntimeError("Langfuse Score config IDs are unavailable")
    values: dict[str, bool | float | str] = {
        "task_success": True,
        "answer_quality": 1.0,
        "review_outcome": "accept",
    }
    for name, value in values.items():
        client.create_score(
            name=name,
            value=value,
            data_type=SCORE_SPECS[name].data_type,
            config_id=config_ids[name],
            trace_id=trace_id,
            metadata={
                "evaluator": "bsagent-synthetic-rule-v1",
                "evaluator_version": "rules-v1",
                "rubric_version": "bsagent-judge-v1",
                "dataset_version": DATASET_VERSION,
                "run_id": "langfuse-example-run-v1",
            },
        )
    return {"trace_id": trace_id, "scores": list(values)}


def _get_or_create_prompt(client: Any) -> Any:
    try:
        return client.get_prompt(PROMPT_NAME, label="production", type="text")
    except Exception as error:
        status = getattr(error, "status_code", None) or getattr(error, "status", None)
        if status != 404:
            raise
    return client.create_prompt(
        name=PROMPT_NAME,
        prompt="Use only this evidence: {{context}}. Answer the question: {{question}}",
        type="text",
        labels=["production"],
        tags=["bsagent", "synthetic", "local"],
        commit_message="Add the stable local synthetic answer prompt.",
    )


def run_prompt(client: Any) -> dict[str, Any]:
    prompt = _get_or_create_prompt(client)
    compiled = prompt.compile(context="Synthetic evidence", question="Synthetic question")
    with client.start_as_current_observation(
        name="bsagent.example-prompt-answer",
        as_type="generation",
        input={"context": "Synthetic evidence", "question": "Synthetic question"},
        output={"answer": "Synthetic answer [synthetic:doc-1]"},
        prompt=prompt,
        model="synthetic-local-model",
        model_parameters={"temperature": 0},
    ):
        pass
    return {
        "prompt": PROMPT_NAME,
        "prompt_version": getattr(prompt, "version", None),
        "compiled": bool(compiled),
    }


def run_evaluator(client: Any, *, deepseek_key: str, base_url: str, model: str) -> dict[str, Any]:
    if not deepseek_key:
        raise ValueError("evaluator example requires DEEPSEEK_API_KEY")
    evaluator = ensure_native_answer_evaluator(client, deepseek_key, base_url, model)
    return {
        "evaluator": getattr(evaluator, "name", "bsagent-answer-quality-v1"),
        "evaluator_id": getattr(evaluator, "id", None),
    }


def run_annotation(
    client: Any,
    *,
    base_url: str,
    public_key: str,
    secret_key: str,
    annotation_client_factory: Any | None = None,
) -> dict[str, Any]:
    trace = run_trace(client)
    config_ids = ensure_score_configs(client)
    if any(not config_ids.get(name) for name in ("answer_quality", "selection_correctness")):
        raise RuntimeError("Langfuse annotation score config IDs are unavailable")
    queue_factory = annotation_client_factory or AnnotationQueueClient
    with queue_factory(
        base_url,
        public_key,
        secret_key,
        score_config_ids=config_ids,
    ) as queues:
        queue_refs = queues.ensure_queues(score_config_ids=config_ids)
        answer_queue = next(q for q in queue_refs if q.name == "answer-quality-review")
        selection_queue = next(q for q in queue_refs if q.name == "skill-tool-selection-review")
        answer_item = queues.enqueue(
            answer_queue.name,
            business_trace_id=trace["trace_id"],
            object_id=trace["trace_id"],
            object_type="TRACE",
            reason="synthetic_example",
            force=True,
        )
        selection_item = queues.enqueue(
            selection_queue.name,
            business_trace_id=trace["trace_id"],
            object_id=trace["selection_observation_id"],
            object_type="OBSERVATION",
            reason="synthetic_example",
            force=True,
        )
    if any(getattr(item, "status", None) != "PENDING" for item in (answer_item, selection_item)):
        raise RuntimeError("Langfuse annotation example items were not created as PENDING")
    return {
        "queues": [answer_queue.name, selection_queue.name],
        "items": [getattr(answer_item, "item_id", None), getattr(selection_item, "item_id", None)],
        "statuses": [getattr(answer_item, "status", None), getattr(selection_item, "status", None)],
    }


def _is_not_found(error: Exception) -> bool:
    return (getattr(error, "status_code", None) or getattr(error, "status", None)) == 404


def run_dataset(client: Any) -> dict[str, Any]:
    try:
        dataset = client.get_dataset(DATASET_NAME)
    except Exception as error:
        if not _is_not_found(error):
            raise
        client.create_dataset(
            name=DATASET_NAME,
            description="Three synthetic, read-only Langfuse evaluation examples.",
            metadata={"dataset_version": DATASET_VERSION, "synthetic_only": True},
        )
        dataset = client.get_dataset(DATASET_NAME)

    known_ids = {
        str(item.id) for item in getattr(dataset, "items", []) if getattr(item, "id", None)
    }
    items = _synthetic_items()
    created = 0
    for item in items:
        item_id = _dataset_item_id(item["id"])
        if item_id in known_ids:
            continue
        client.create_dataset_item(
            dataset_name=DATASET_NAME,
            id=item_id,
            input=item["input"],
            expected_output=item["expected_output"],
            metadata={
                "example_id": item["id"],
                "allowed_tools": item["allowed_tools"],
                "expected_citations": item["expected_citations"],
                "risk_level": item["risk_level"],
                "dataset_version": DATASET_VERSION,
                "synthetic_only": True,
            },
        )
        created += 1
    return {
        "dataset": DATASET_NAME,
        "dataset_version": DATASET_VERSION,
        "items": len(items),
        "created": created,
    }


def _rule_evaluator(
    *, input: Any, output: Any, expected_output: Any = None, metadata: Any = None, **_kwargs: Any
) -> list[dict[str, Any]]:
    data = metadata if isinstance(metadata, Mapping) else {}
    allowed = data.get("allowed_tools", [])
    expected_citations = data.get("expected_citations", [])
    output_text = json.dumps(output, ensure_ascii=False)
    citations_found = sum(citation in output_text for citation in expected_citations)
    rule_result = evaluate_rules(
        RuleEvaluationInput(
            completed=bool(output),
            claims_total=len(expected_citations),
            claims_with_citations=citations_found,
            high_risk_actions=1 if data.get("risk_level") == "high" else 0,
            approved_high_risk_actions=0,
            selected_tool=allowed[0] if allowed else None,
            expected_tools=tuple(allowed),
        )
    )
    return [
        {"name": name, "value": value, "comment": "Synthetic deterministic rule evaluation."}
        for name, value in rule_result.scores.items()
    ]


def run_experiment(
    client: Any,
    *,
    with_deepseek: bool = False,
    deepseek_key: str = "",
    base_url: str = "https://api.deepseek.com",
    model: str = "deepseek-v4-flash",
) -> dict[str, Any]:
    run_dataset(client)
    dataset = client.get_dataset(DATASET_NAME)
    prompt = _get_or_create_prompt(client)
    metadata = {
        "prompt_name": PROMPT_NAME,
        "prompt_version": str(getattr(prompt, "version", "unknown")),
        "graph_version": GRAPH_VERSION,
        "skill_version": SKILL_VERSION,
        "knowledge_version": KNOWLEDGE_VERSION,
        "dataset_version": DATASET_VERSION,
        "evaluator_version": "bsagent-rule-evaluator-v1",
    }

    def task(*, item: Any, **_kwargs: Any) -> dict[str, Any]:
        source = item.input if hasattr(item, "input") else item["input"]
        example_id = getattr(item, "id", None) or (item.get("metadata", {}) or {}).get(
            "example_id", "synthetic"
        )
        question = source.get("question", "Synthetic question")
        expected = getattr(item, "expected_output", None)
        if expected is None and isinstance(item, Mapping):
            expected = item.get("expected_output")
        answer = expected or f"Synthetic answer for {example_id}"
        compiled_prompt = prompt.compile(
            context="Synthetic evidence from the fixed local knowledge snapshot.",
            question=question,
        )
        with client.start_as_current_observation(
            name="bsagent.example-experiment-generation",
            as_type="generation",
            input={"prompt": compiled_prompt},
            output={"answer": answer},
            prompt=prompt,
            model="synthetic-local-model",
            model_parameters={"temperature": 0},
        ):
            pass
        return {"answer": answer, "synthetic": True, "question": question}

    evaluators = [_rule_evaluator]
    if with_deepseek:
        if not deepseek_key:
            raise ValueError("--with-deepseek requires DEEPSEEK_API_KEY")
        ensure_native_answer_evaluator(client, deepseek_key, base_url, model)
        from app.llm.deepseek import DeepSeekClient

        judge = DeepSeekJudge(DeepSeekClient(api_key=deepseek_key, base_url=base_url, model=model))

        async def deepseek_evaluator(
            *, input: Any, output: Any, expected_output: Any = None, **_kwargs: Any
        ) -> dict[str, Any]:
            result = await judge.evaluate(
                input_text=json.dumps(input, ensure_ascii=False),
                output_text=json.dumps(output, ensure_ascii=False),
                expected_output=json.dumps(expected_output, ensure_ascii=False),
            )
            if not hasattr(result, "answer_quality"):
                return {"name": "deepseek_judge_status", "value": "failed", "comment": result.code}
            return {
                "name": "answer_quality",
                "value": result.answer_quality,
                "comment": "DeepSeek judge; synthetic dataset.",
            }

        evaluators.append(deepseek_evaluator)
        metadata["judge"] = "deepseek-explicit-v1"
    result = dataset.run_experiment(
        name="bsagent-local-rule-evaluation-v1",
        description="Sequential rule evaluation over the stable synthetic dataset.",
        task=task,
        evaluators=evaluators,
        max_concurrency=1,
        metadata=metadata,
    )
    return {
        "experiment": "bsagent-local-rule-evaluation-v1",
        "run_name": getattr(result, "run_name", None),
    }


def _load_client_and_settings() -> tuple[Any, Any]:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env.local", override=False)
    from app.core.config import Settings

    from langfuse import Langfuse

    settings = Settings()
    if not settings.langfuse_enabled:
        raise RuntimeError("Langfuse is not enabled in local settings")
    client = Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        base_url=settings.langfuse_base_url,
        environment=settings.app_env,
    )
    return client, settings


def _run_command(
    command: str, client: Any, settings: Any, *, with_deepseek: bool
) -> dict[str, Any]:
    if command == "trace":
        return run_trace(client)
    if command == "score":
        return run_score(client)
    if command == "prompt":
        return run_prompt(client)
    if command == "evaluator":
        return run_evaluator(
            client,
            deepseek_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            model=settings.deepseek_model,
        )
    if command == "annotation":
        return run_annotation(
            client,
            base_url=settings.langfuse_base_url,
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
        )
    if command == "dataset":
        return run_dataset(client)
    if command == "experiment":
        return run_experiment(
            client,
            with_deepseek=with_deepseek,
            deepseek_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            model=settings.deepseek_model,
        )
    if command == "all":
        trace = run_trace(client)
        score = run_score(client, trace["trace_id"])
        return {
            "trace": trace,
            "score": score,
            "prompt": run_prompt(client),
            "evaluator": run_evaluator(
                client,
                deepseek_key=settings.deepseek_api_key,
                base_url=settings.deepseek_base_url,
                model=settings.deepseek_model,
            ),
            "annotation": run_annotation(
                client,
                base_url=settings.langfuse_base_url,
                public_key=settings.langfuse_public_key,
                secret_key=settings.langfuse_secret_key,
            ),
            "dataset": run_dataset(client),
            "experiment": run_experiment(
                client,
                with_deepseek=with_deepseek,
                deepseek_key=settings.deepseek_api_key,
                base_url=settings.deepseek_base_url,
                model=settings.deepseek_model,
            ),
        }
    raise ValueError("unknown example command")


def _safe_summary(value: Any, base_url: str) -> dict[str, Any]:
    """Restrict terminal output to resource IDs/names, local links, and pass indicators."""
    if isinstance(value, Mapping):
        summary: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(item, Mapping):
                summary[key] = _safe_summary(item, base_url)
            elif (
                key
                in {
                    "trace_id",
                    "root_observation_id",
                    "selection_observation_id",
                    "evaluator_id",
                    "run_name",
                }
                or key in {"prompt", "dataset", "experiment", "evaluator", "queues", "scores"}
                or key == "items"
                and isinstance(item, list)
                or key == "statuses"
                and isinstance(item, list)
            ):
                summary[key] = item
            elif key == "compiled" and item is True:
                summary.setdefault("passed_checks", []).append("prompt_compiled")
        summary["local_url"] = base_url.rstrip("/")
        summary["passed"] = True
        return summary
    return {"passed": True, "local_url": base_url.rstrip("/")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "trace",
            "score",
            "prompt",
            "evaluator",
            "annotation",
            "dataset",
            "experiment",
            "all",
        ),
    )
    parser.add_argument("--with-deepseek", action="store_true", help="显式启用 DeepSeek Judge")
    args = parser.parse_args(argv)
    client = None
    settings = None
    try:
        client, settings = _load_client_and_settings()
        result = _run_command(args.command, client, settings, with_deepseek=args.with_deepseek)
        print(
            json.dumps(
                _safe_summary(result, settings.langfuse_base_url),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        flush = getattr(client, "flush", None)
        if callable(flush):
            flush()
        return 0
    except Exception:  # noqa: BLE001 - SDK 异常可能携带请求正文或凭证
        # Do not relay SDK errors: they may contain request details or configured credentials.
        base_url = getattr(settings, "langfuse_base_url", "http://localhost:3000")
        print(
            json.dumps(
                {"error": "langfuse_example_failed", "local_url": base_url, "passed": False},
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
