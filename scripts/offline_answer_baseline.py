"""以现有 DeepSeek 云端模型离线评审真实历史回答，不重放业务工具。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import Settings
from app.llm.deepseek import LlmProviderError
from app.llm.schemas import LlmCompletionResult

from scripts.real_eval_dataset import decode_io, digest, validate_items

VERSION = "historical-answer-baseline-2026-11-01-v4"
PROMPT_PATH = ROOT / "docs/evaluation/offline-answer-judge-v1.md"
DIMENSIONS = (
    "requirement_satisfaction",
    "factual_grounding",
    "reasoning_correctness",
    "citation_support",
    "presentation_quality",
)
Verdict = Literal["pass", "partial", "fail", "not_assessable"]
DimensionName = Literal[
    "requirement_satisfaction",
    "factual_grounding",
    "reasoning_correctness",
    "citation_support",
    "presentation_quality",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Assessment(StrictModel):
    verdict: Verdict
    reason: str = Field(min_length=1, max_length=1500)


class Dimensions(StrictModel):
    requirement_satisfaction: Assessment
    factual_grounding: Assessment
    reasoning_correctness: Assessment
    citation_support: Assessment
    presentation_quality: Assessment


class Issue(StrictModel):
    dimension: DimensionName
    severity: Literal["critical", "major", "minor"]
    kind: Literal["contradiction", "unsupported", "reasoning", "omission", "presentation"]
    answer_quote: str = Field(max_length=1800)
    evidence_quote: str = Field(max_length=1800)
    finding: str = Field(min_length=1, max_length=1800)
    origin: Literal[
        "router",
        "planner",
        "tool_retrieval",
        "tool_arguments",
        "tool_execution",
        "context_projection",
        "synthesis",
        "unknown",
    ] = "unknown"


class RequirementCheck(StrictModel):
    question_quote: str = Field(min_length=1, max_length=1800)
    answer_quote: str = Field(max_length=1800)
    status: Literal["answered", "missing", "incorrect"]
    reason: str = Field(min_length=1, max_length=1500)


class Judgment(StrictModel):
    dimensions: Dimensions
    issues: list[Issue] = Field(max_length=5)
    requirement_checks: list[RequirementCheck] | None = Field(default=None, max_length=12)
    summary: str = Field(
        default="评审器未返回摘要，结论见各维度及逐条问题。", min_length=1, max_length=1500
    )


class JsonJudgeClient:
    """独立离线客户端，启用官方 JSON 输出；不改业务模型客户端。"""

    def __init__(self, http: Any, *, model: str, api_key: str):
        self.http = http
        self.model = model
        self.api_key = api_key

    async def complete(self, *, system_prompt, user_prompt, max_tokens, **_kwargs):
        response = await self.http.post(
            "/chat/completions",
            headers={"Authorization": "Bearer " + self.api_key},
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0,
                "max_tokens": max_tokens,
                "thinking": {"type": "disabled"},
                "response_format": {"type": "json_object"},
            },
        )
        response.raise_for_status()
        data = response.json()
        try:
            choice = data["choices"][0]
            return LlmCompletionResult(
                content=choice["message"]["content"],
                model=data.get("model", self.model),
                provider="deepseek",
                usage=data.get("usage") or {},
                finish_reason=choice.get("finish_reason"),
            )
        except (IndexError, KeyError, TypeError) as error:
            raise ValueError("invalid_provider_response") from error


def prepare_payload(item: dict, answer_override: str | None = None) -> dict:
    """Judge 只看事实材料；旧回答仅为待评输出，不传历史自评与旧模型解释。"""
    source = item["input"]
    ready = item["metadata"]["baseline_groundedness_ready"]
    turns = source.get("synthesis_turns") or []
    payload = decode_io(turns[-1]["input"]) if turns and ready else {}
    if isinstance(payload, dict) and "user_prompt" in payload:
        payload = decode_io(payload["user_prompt"])
    if not isinstance(payload, dict):
        payload = {}
    # 只选模型实际看到的研究材料和来源；不把计划、goal、自评和缺失清单升级为事实。
    materials = {
        k: payload[k]
        for k in (
            "research_materials",
            "sources",
            "evidence",
            "field_semantics",
            "request_context",
            "user_message",
            "data_limitations",
        )
        if k in payload
    }
    answer = (item["metadata"].get("baseline_output") or {}).get("content", "")
    if answer_override is not None:
        answer = answer_override
    return {
        "question": source["question"],
        "as_of_utc": source["as_of_utc"],
        "timezone": source["timezone"],
        "requirements": item["expected_output"],
        "answer_to_evaluate": answer,
        "full_synthesis_input_available": ready,
        "integrity_flags": item["metadata"]["integrity_flags"],
        "actual_materials": materials if ready else None,
        "evaluation_scope": "历史回答相对原问及已记录实际输入的质量；不独立验证外部事实，不评当前部署版本准确率。",
    }


def text_leaves(value: Any, prefix: str = "") -> list[tuple[str, str]]:
    if isinstance(value, dict):
        return [(f"{prefix}/{k}/@key", str(k)) for k in value] + [
            (path, text) for k, v in value.items() for path, text in text_leaves(v, f"{prefix}/{k}")
        ]
    if isinstance(value, list):
        return [
            (path, text)
            for i, v in enumerate(value)
            for path, text in text_leaves(v, f"{prefix}/{i}")
        ]
    if value is None:
        return []
    return [(prefix, str(value))]


def validate_judgment(value: Any, payload: dict) -> dict:
    result = Judgment.model_validate(value).model_dump()
    checks = result.get("requirement_checks")
    if checks is None:
        result.pop("requirement_checks", None)
    else:
        for index, check in enumerate(checks):
            if check["question_quote"] not in payload["question"]:
                raise ValueError(f"question_quote_not_found:requirement_checks[{index}]")
            quote = check["answer_quote"]
            if quote and quote not in payload["answer_to_evaluate"]:
                raise ValueError(f"answer_quote_not_found:requirement_checks[{index}]")
            if not quote and check["status"] != "missing":
                raise ValueError("missing_core_result_answer_quote")
    available = payload["full_synthesis_input_available"]
    if not available:
        for dim in ("factual_grounding", "citation_support"):
            if result["dimensions"][dim]["verdict"] != "not_assessable":
                raise ValueError("incomplete_evidence_was_scored")
    evidence = text_leaves(
        {"question": payload["question"], "actual_materials": payload["actual_materials"]}
    )
    for index, issue in enumerate(result["issues"]):
        quote = issue["answer_quote"]
        if not quote and issue["kind"] != "omission":
            raise ValueError("missing_answer_quote")
        if quote and quote not in payload["answer_to_evaluate"]:
            raise ValueError(f"answer_quote_not_found:issues[{index}].answer_quote")
        if not available and issue["dimension"] in {"factual_grounding", "citation_support"}:
            raise ValueError("incomplete_evidence_issue")
        quote = issue["evidence_quote"]
        candidates = evidence
        if issue["kind"] in {"omission", "reasoning", "presentation"}:
            candidates = evidence + text_leaves(payload["requirements"], "/requirements")
        if issue["kind"] in {"reasoning", "presentation"}:
            candidates = candidates + [("/answer_to_evaluate", payload["answer_to_evaluate"])]
        refs = [path for path, text in candidates if quote and quote in text]
        if quote and not refs:
            raise ValueError(f"evidence_quote_not_found:issues[{index}].evidence_quote")
        issue["verified_evidence_paths"] = refs
        issue["answer_quote_verified"] = bool(issue["answer_quote"])
        issue["origin_status"] = "unverified_hypothesis"
    if (
        any(x["verdict"] in {"partial", "fail"} for x in result["dimensions"].values())
        and not result["issues"]
    ):
        raise ValueError("negative_verdict_without_issue")
    return result


def parse_judge_json(content: str) -> tuple[dict, str | None]:
    """只修复已观察到的根对象后游离 summary，不丢弃或拼改任何判定字段。"""
    decoder = json.JSONDecoder()
    value, end = decoder.raw_decode(content.strip())
    tail = content.strip()[end:].strip()
    if not isinstance(value, dict):
        raise TypeError("judge_response_not_object")
    if not tail:
        return value, None
    if tail.startswith(",") and "summary" not in value:
        extra = json.loads("{" + tail[1:])
        if (
            isinstance(extra, dict)
            and set(extra) == {"summary"}
            and isinstance(extra["summary"], str)
        ):
            return {**value, **extra}, "detached_summary"
    raise ValueError("ambiguous_trailing_json")


async def judge_one(
    client: Any,
    item: dict,
    prompt: str,
    *,
    answer_override: str | None = None,
    validation_hook=None,
    repair_context: dict | None = None,
    max_attempts: int = 2,
) -> dict:
    if type(max_attempts) is not int or max_attempts not in {1, 2}:
        raise ValueError("评审仅允许一次生成及至多一次协议修复")
    payload = prepare_payload(item, answer_override)
    started = perf_counter()
    errors = []
    diagnostics = []
    usage = {"input_tokens": 0, "output_tokens": 0}
    previous_output = None
    for attempt in range(max_attempts):
        suffix = (
            ""
            if not errors
            else "\n上次输出未通过结构或逐字引文校验："
            + errors[-1]
            + "。请重新核对并返回一个完整 JSON。evidence_quote只可从问题或actual_materials选一个连续短片段；找不到原文时填空串，不把计算公式或改写当引文。"
        )
        request_payload = payload
        if validation_hook is not None and (repair_context or errors):
            context = deepcopy(repair_context) if repair_context else {}
            if previous_output is not None:
                context["previous_output"] = previous_output
            context["validation_errors"] = errors or context.get("validation_errors", [])
            request_payload = {**payload, "protocol_repair": context}
        try:
            response = await client.complete(
                system_prompt=prompt + suffix,
                user_prompt=json.dumps(request_payload, ensure_ascii=False),
                thinking=False,
                temperature=0,
                max_tokens=2600,
            )
            previous_output = response.content
            for key, alias in (
                ("input_tokens", "prompt_tokens"),
                ("output_tokens", "completion_tokens"),
            ):
                usage[key] += (response.usage or {}).get(key, (response.usage or {}).get(alias, 0))
            if response.finish_reason == "length":
                raise ValueError("judge_output_truncated")
            diagnostics.append({"attempt": attempt + 1, "raw_response": response.content})
            raw, repair = parse_judge_json(response.content)
            diagnostics.append({"attempt": attempt + 1, "response": raw})
            judgment = validate_judgment(raw, payload)
            if validation_hook is not None:
                validation_hook(judgment)
            result = {
                "status": "success",
                "judgment": judgment,
                "attempts": attempt + 1,
                "usage": usage,
                "duration_seconds": round(perf_counter() - started, 3),
                "model": response.model,
                "validation_errors": errors,
                "format_repair": repair,
            }
            if validation_hook is not None:
                result["diagnostics"] = diagnostics
            return result
        except (ValueError, TypeError, LlmProviderError, httpx.HTTPError, TimeoutError) as error:
            # 仅记录错误类型/稳定校验码，不输出 provider 正文、密钥或业务原始材料。
            errors.append(
                str(error)
                if isinstance(error, ValueError)
                and not hasattr(error, "errors")
                and len(str(error)) < 100
                else getattr(error, "code", type(error).__name__)
            )
            if hasattr(error, "errors"):
                diagnostics.append(
                    {
                        "attempt": attempt + 1,
                        "schema_errors": [
                            {"loc": list(e["loc"]), "type": e["type"]} for e in error.errors()
                        ],
                    }
                )
    return {
        "status": "error",
        "error_codes": errors,
        "attempts": max_attempts,
        "usage": usage,
        "duration_seconds": round(perf_counter() - started, 3),
        "diagnostics": diagnostics,
    }


def aggregate(results: list[dict]) -> dict:
    successful = [x for x in results if x["status"] == "success"]
    return {
        "total": len(results),
        "successful": len(successful),
        "evaluation_errors": len(results) - len(successful),
        "dimensions": {
            dim: dict(Counter(x["judgment"]["dimensions"][dim]["verdict"] for x in successful))
            for dim in DIMENSIONS
        },
        "issue_severity": dict(
            Counter(issue["severity"] for x in successful for issue in x["judgment"]["issues"])
        ),
        "usage": {
            k: sum(x.get("usage", {}).get(k, 0) for x in results)
            for k in ("input_tokens", "output_tokens")
        },
        "interpretation": "模型初评，不是人工金标；历史记录跨版本，未估计当前部署准确率；不可评与调用失败不计作质量零分。",
    }


async def run_batch(folder: Path, *, families: list[str] | None, controls: bool = False) -> dict:
    items = json.loads((folder / "items.json").read_text())
    validate_items(items)
    if families:
        items = [x for x in items if x["metadata"]["family"] in families]
    if controls and any(x["metadata"]["split"] != "development" for x in items):
        raise ValueError("holdout_must_not_be_used_for_calibration")
    settings = Settings(_env_file=ROOT / ".env.local")
    prompt = PROMPT_PATH.read_text()
    output = folder / VERSION / ("calibration" if controls else "baseline")
    output.mkdir(parents=True, exist_ok=True)
    overrides = json.loads((folder / VERSION / "controls.json").read_text()) if controls else {}
    semaphore = asyncio.Semaphore(3)
    async with httpx.AsyncClient(
        base_url=settings.deepseek_base_url, timeout=100, trust_env=False
    ) as http:
        client = JsonJudgeClient(
            http, api_key=settings.deepseek_api_key, model=settings.deepseek_model
        )

        async def evaluate(item):
            meta = item["metadata"]
            key = meta["source_trace_id"]
            override = overrides.get(meta["family"])
            fingerprint = digest(
                {
                    "item_sha256": meta["artifact_sha256"],
                    "prompt": prompt,
                    "model": settings.deepseek_model,
                    "answer_override": override,
                    "thinking": False,
                    "max_tokens": 2600,
                }
            )
            path = output / f"{key}.json"
            if path.exists():
                cached = json.loads(path.read_text())
                if cached.get("fingerprint") != fingerprint:
                    raise ValueError("评审输入发生变化，请使用新的运行版本或独立输出目录")
                if cached["status"] == "success":
                    return cached
            async with semaphore:
                result = await judge_one(client, item, prompt, answer_override=override)
            result.update(
                {
                    "item_id": item["id"],
                    "source_trace_id": key,
                    "source_observation_id": meta["source_observation_id"],
                    "split": meta["split"],
                    "family": meta["family"],
                    "fingerprint": fingerprint,
                    "evaluator_version": VERSION,
                    "prompt_sha256": digest(prompt),
                    "is_counterfactual_control": controls,
                    "human_reviewed": False,
                }
            )
            path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            print(
                json.dumps(
                    {
                        "family": meta["family"],
                        "status": result["status"],
                        "attempts": result["attempts"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return result

        results = await asyncio.gather(*(evaluate(x) for x in items))
    report = {
        **aggregate(results),
        "evaluator_version": VERSION,
        "model": settings.deepseek_model,
        "prompt_sha256": digest(prompt),
        "completed_at": datetime.now(UTC).isoformat(),
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["calibrate", "evaluate"])
    parser.add_argument("--folder", type=Path, default=ROOT / "data/evaluation/2026-11-01")
    parser.add_argument("--families", nargs="+")
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(
                run_batch(args.folder, families=args.families, controls=args.command == "calibrate")
            ),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
