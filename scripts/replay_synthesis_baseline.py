"""固定历史模型输入，回放当前合成提示词；不执行业务图或真实工具。"""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
import sys
from copy import deepcopy
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import Settings
from app.graph.nodes.synthesizer import _ensure_financial_disclaimer, _strip_model_brand
from app.graph.planning.answer_citations import AnswerCitations
from app.graph.planning.synthesis_decision import (
    REPLAN_PROMPT,
    is_answer_header,
    parse_synthesis_decision,
)
from app.llm.prompts import BUSINESS_LANGUAGE_INSTRUCTION, EVIDENCE_VERIFICATION_INSTRUCTION

from scripts.offline_answer_baseline import (
    PROMPT_PATH,
    JsonJudgeClient,
    aggregate,
    judge_one,
)
from scripts.offline_answer_baseline import (
    VERSION as JUDGE_VERSION,
)
from scripts.publish_offline_baseline import publish, score_records
from scripts.real_eval_dataset import decode_io, digest, has_truncation, validate_items

VERSION = "synthesis-fixed-input-2026-11-01-v1"
SOURCE = ROOT / "backend/app/graph/nodes/synthesizer.py"


@lru_cache(maxsize=1)
def render_ast():
    tree = ast.parse(SOURCE.read_text())
    return next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_render_answer"
    )


def evaluate_source_expression(node, **values):
    # 只求值本机受控源码中的提示词/参数表达式，不编译用户或模型返回文本。
    return eval(compile(ast.Expression(node), str(SOURCE), "eval"), {"__builtins__": {}}, values)


def current_system_prompt(payload: dict) -> str:
    function = render_ast()
    assignment = next(
        n
        for n in function.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "system_prompt" for t in n.targets)
    )
    env = {
        "EVIDENCE_VERIFICATION_INSTRUCTION": EVIDENCE_VERIFICATION_INSTRUCTION,
        "REPLAN_PROMPT": REPLAN_PROMPT,
    }
    prompt = evaluate_source_expression(assignment.value, **env)
    conditions = {
        "new_flow": "required_schema" in payload,
        "projection.sources": bool(payload.get("sources")),
        "handoff is not None": bool(payload.get("answer_requirements")),
    }
    found = []
    for branch in function.body:
        if not isinstance(branch, ast.If):
            continue
        appends = [
            n
            for n in ast.walk(branch)
            if isinstance(n, ast.AugAssign)
            and isinstance(n.target, ast.Name)
            and n.target.id == "system_prompt"
        ]
        if not appends:
            continue
        condition = ast.unparse(branch.test)
        if condition not in conditions or len(appends) != 1:
            raise ValueError("当前提示词结构变化，需要重新核对提取规则")
        found.append(condition)
        if conditions[condition]:
            prompt += evaluate_source_expression(appends[0].value, **env)
    if set(found) != set(conditions):
        raise ValueError("未完整提取当前条件提示词")
    # invoke_llm_completion 的实际公共追加内容，避免漏掉业务表达规则。
    return prompt + "\n\n" + BUSINESS_LANGUAGE_INSTRUCTION


def generation_request(item: dict) -> dict:
    if not item["metadata"]["baseline_groundedness_ready"]:
        raise ValueError("仅接受完整合成输入")
    turn = item["input"]["synthesis_turns"][-1]
    captured = decode_io(turn["input"])
    payload = deepcopy(decode_io(captured["user_prompt"]))
    if not isinstance(payload, dict) or has_truncation(payload):
        raise ValueError("合成输入不完整")
    completion = next(
        n
        for n in render_ast().body
        if isinstance(n, ast.Assign)
        and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Name)
        and n.value.func.id == "complete_answer"
    )
    options = {
        k.arg: evaluate_source_expression(k.value, handoff=payload.get("answer_requirements"))
        for k in completion.value.keywords
        if k.arg in {"temperature", "thinking", "max_tokens"}
    }
    return {"system_prompt": current_system_prompt(payload), "payload": payload, **options}


def parse_response(raw: str, payload: dict) -> dict:
    if "required_schema" in payload:
        decision = parse_synthesis_decision(raw)
        if decision.action == "request_replan":
            if not payload.get("allow_replan"):
                raise ValueError("补证被禁用")
            return {
                "action": "request_replan",
                "replan_request": decision.replan_request.model_dump(mode="json"),
            }
        text = decision.answer
    else:
        text = raw.strip()
        if not text:
            raise ValueError("回答为空")
    return {
        "action": "answer",
        "answer": _ensure_financial_disclaimer(
            AnswerCitations(payload.get("sources") or []).render(_strip_model_brand(text))
        ),
    }


class StreamingClient:
    def __init__(self, http, *, api_key, model):
        self.http, self.api_key, self.model = http, api_key, model

    async def complete(self, *, system_prompt, user_prompt, temperature, max_tokens, thinking):
        started = perf_counter()
        first = None
        chunks, usage, finish, model = [], {}, None, self.model
        async with self.http.stream(
            "POST",
            "/chat/completions",
            headers={"Authorization": "Bearer " + self.api_key},
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": temperature,
                "max_tokens": max_tokens,
                "thinking": {"type": "enabled" if thinking else "disabled"},
                "stream": True,
                "stream_options": {"include_usage": True},
            },
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                packet = json.loads(data)
                model = packet.get("model", model)
                usage = packet.get("usage") or usage
                for choice in packet.get("choices") or []:
                    delta = choice.get("delta", {}).get("content")
                    if delta:
                        if first is None:
                            first = perf_counter() - started
                        chunks.append(delta)
                    finish = choice.get("finish_reason") or finish
        if finish is None:
            raise ValueError("stream_incomplete")
        return SimpleNamespace(
            content="".join(chunks),
            model=model,
            usage=usage,
            finish_reason=finish,
            first_token_seconds=first,
            duration_seconds=perf_counter() - started,
        )


async def replay_one(client, request: dict) -> dict:
    payload = deepcopy(request["payload"])
    attempts, usage = [], {"input_tokens": 0, "output_tokens": 0}
    for attempt in range(2):
        try:
            result = await client.complete(
                system_prompt=request["system_prompt"],
                user_prompt=json.dumps(payload, ensure_ascii=False),
                temperature=request["temperature"] if not attempt else 0.0,
                max_tokens=request["max_tokens"] if not attempt else 6144,
                thinking=False,
            )
            for key, alias in [
                ("input_tokens", "prompt_tokens"),
                ("output_tokens", "completion_tokens"),
            ]:
                usage[key] += result.usage.get(key, result.usage.get(alias, 0))
            attempts.append(
                {
                    "raw_response": result.content,
                    "finish_reason": result.finish_reason,
                    "first_token_seconds": result.first_token_seconds,
                    "duration_seconds": result.duration_seconds,
                }
            )
            if result.finish_reason == "length":
                raise ValueError("output_truncated")
            parsed = parse_response(result.content, payload)
            return {
                "status": "success",
                **parsed,
                "attempts": attempts,
                "usage": usage,
                "model": result.model,
                "first_token_seconds": attempts[0]["first_token_seconds"],
                "generation_seconds": sum(a["duration_seconds"] for a in attempts),
            }
        except (ValueError, httpx.HTTPError) as error:
            code = type(error).__name__ if isinstance(error, httpx.HTTPError) else str(error)[:120]
            if (
                not attempts
                or "raw_response" not in attempts[-1]
                or isinstance(error, httpx.HTTPError)
            ):
                diagnostics = {}
                if isinstance(error, httpx.HTTPStatusError):
                    diagnostics["http_status"] = error.response.status_code
                    try:
                        provider_type = (error.response.json().get("error") or {}).get("type")
                        if isinstance(provider_type, str) and len(provider_type) < 100:
                            diagnostics["provider_error_type"] = provider_type
                    except (ValueError, AttributeError):
                        pass
                return {
                    "status": "error",
                    "error": code,
                    "attempts": attempts,
                    "usage": usage,
                    **diagnostics,
                }
            attempts[-1]["validation_error"] = code
            payload["format_repair"] = (
                "上次输出不符合决策与正文协议，请重新按 required_schema 输出；不补造事实。"
            )
            header = result.content.strip().partition("\n")[0]
            if is_answer_header(header):
                from app.graph.planning.synthesis_decision import decision_schema

                payload.update(allow_replan=False, required_schema=decision_schema(False))
    return {"status": "error", "error": "protocol_invalid", "attempts": attempts, "usage": usage}


async def run(folder: Path, command: str, families=None):
    items = json.loads((folder / "items.json").read_text())
    validate_items(items)
    selected = [
        i
        for i in items
        if i["metadata"]["baseline_groundedness_ready"]
        and (not families or i["metadata"]["family"] in families)
    ]
    settings = Settings(_env_file=ROOT / ".env.local")
    dest = folder / VERSION
    dest.mkdir(exist_ok=True)
    prompt = PROMPT_PATH.read_text()
    semaphore = asyncio.Semaphore(3)
    async with httpx.AsyncClient(
        base_url=settings.deepseek_base_url, timeout=180, trust_env=False
    ) as http:
        generator = StreamingClient(
            http, api_key=settings.deepseek_api_key, model=settings.deepseek_model
        )
        judge = JsonJudgeClient(
            http, api_key=settings.deepseek_api_key, model=settings.deepseek_model
        )

        async def process(item):
            trace = item["metadata"]["source_trace_id"]
            path = dest / "generations" / f"{trace}.json"
            path.parent.mkdir(exist_ok=True)
            request = generation_request(item)
            fingerprint = digest(
                {
                    "request": request,
                    "model": settings.deepseek_model,
                    "source_sha": digest(SOURCE.read_text()),
                }
            )
            if path.exists():
                generation = json.loads(path.read_text())
                if generation["fingerprint"] != fingerprint:
                    raise ValueError("回放输入发生变化，请使用新的运行版本")
            elif command == "generate":
                async with semaphore:
                    generation = await replay_one(generator, request)
                generation.update(
                    item_id=item["id"],
                    source_trace_id=trace,
                    source_observation_id=item["metadata"]["source_observation_id"],
                    family=item["metadata"]["family"],
                    split=item["metadata"]["split"],
                    fingerprint=fingerprint,
                    replay_version=VERSION,
                    request=request,
                    historical_model=item["input"]["synthesis_turns"][-1]["model"],
                )
                path.write_text(json.dumps(generation, ensure_ascii=False, indent=2) + "\n")
                print(
                    json.dumps(
                        {
                            "family": generation["family"],
                            "status": generation["status"],
                            "action": generation.get("action"),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            else:
                raise ValueError("缺少生成结果")
            if (
                command == "evaluate"
                and generation.get("action") == "answer"
                and generation["status"] == "success"
            ):
                out = dest / "judgments" / f"{trace}.json"
                out.parent.mkdir(exist_ok=True)
                key = digest(
                    {
                        "generation": generation["answer"],
                        "judge_prompt": prompt,
                        "model": settings.deepseek_model,
                    }
                )
                if out.exists():
                    if json.loads(out.read_text())["fingerprint"] != key:
                        raise ValueError("评审输入变化，禁止覆盖")
                else:
                    async with semaphore:
                        result = await judge_one(
                            judge, item, prompt, answer_override=generation["answer"]
                        )
                    result.update(
                        item_id=item["id"],
                        source_trace_id=trace,
                        source_observation_id=item["metadata"]["source_observation_id"],
                        family=item["metadata"]["family"],
                        split=item["metadata"]["split"],
                        fingerprint=key,
                        evaluator_version=JUDGE_VERSION,
                        prompt_sha256=digest(prompt),
                        is_counterfactual_control=False,
                        human_reviewed=False,
                        evaluated_answer_sha256=digest(generation["answer"]),
                    )
                    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
                    print(
                        json.dumps(
                            {"family": result["family"], "evaluation_status": result["status"]},
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
            return generation

        generations = await asyncio.gather(*(process(i) for i in selected))
    if command == "evaluate":
        results = [json.loads(p.read_text()) for p in (dest / "judgments").glob("*.json")]
        summary = aggregate(results)
    else:
        from collections import Counter

        summary = {
            "selected": len(selected),
            "statuses": dict(Counter(g["status"] for g in generations)),
            "actions": dict(Counter(g.get("action", "error") for g in generations)),
        }
    summary.update(
        version=VERSION,
        model=settings.deepseek_model,
        scope="固定模型输入的提示词回放，非完整节点或全链路重放",
        completed_at=datetime.now(UTC).isoformat(),
    )
    (dest / f"{command}-report.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    )
    return summary


def replay_score_records(item, result, generation, review=None):
    metadata = {
        "replay_version": VERSION,
        "generation_fingerprint": generation["fingerprint"],
        "source_trace_id": item["metadata"]["source_trace_id"],
        "source_observation_id": item["metadata"]["source_observation_id"],
        "scope": "fixed_input_current_prompt_replay",
        "human_reviewed": False,
        "not_end_to_end_accuracy": True,
    }
    records = []
    for name, value in (
        ("generation_valid", float(generation["status"] == "success")),
        ("answer_available", float(generation.get("action") == "answer")),
    ):
        records.append(
            {
                "name": "synthesis_replay_v1." + name,
                "value": value,
                "data_type": "NUMERIC",
                "score_id": str(uuid5(NAMESPACE_URL, f"{VERSION}:{item['id']}:{name}")),
                "trace_id": metadata["source_trace_id"],
                "observation_id": metadata["source_observation_id"],
                "environment": "offline-evaluation",
                "metadata": dict(metadata),
                "comment": "只表示生成状态或是否产生最终回答；补证申请不等于质量失败。",
            }
        )
    if generation.get("action") != "answer":
        return records
    if result is None or result.get("evaluated_answer_sha256") != digest(generation["answer"]):
        raise ValueError("评审没有对应当前回放回答")
    for score in score_records(item, result, review):
        name = score["name"].split(".")[-1]
        score["name"] = (
            "synthesis_replay_v1." + ("" if name == "evaluation_valid" else "model.") + name
        )
        score["score_id"] = str(uuid5(NAMESPACE_URL, f"{VERSION}:{item['id']}:{name}"))
        score["metadata"].update(metadata)
        records.append(score)
    return records


def publish_replay(folder: Path):
    from langfuse import Langfuse

    items = [
        i
        for i in json.loads((folder / "items.json").read_text())
        if i["metadata"]["baseline_groundedness_ready"]
    ]
    dest = folder / VERSION
    generations = {
        i["id"]: json.loads(
            (dest / "generations" / (i["metadata"]["source_trace_id"] + ".json")).read_text()
        )
        for i in items
    }
    reviews = json.loads((dest / "review-overlay.json").read_text())["cases"]
    results = []
    for item in items:
        generation = generations[item["id"]]
        if (
            generation["item_id"] != item["id"]
            or generation["source_trace_id"] != item["metadata"]["source_trace_id"]
        ):
            raise ValueError("回放来源不一致")
        if generation.get("action") == "answer":
            result = json.loads(
                (dest / "judgments" / (item["metadata"]["source_trace_id"] + ".json")).read_text()
            )
        else:
            result = {
                "item_id": item["id"],
                "status": "not_applicable",
                "source_trace_id": item["metadata"]["source_trace_id"],
                "generation_fingerprint": generation["fingerprint"],
            }
        results.append(result)

    def scores(item, result, review):
        return replay_score_records(item, result, generations[item["id"]], review)

    def output(item):
        generation = generations[item["id"]]
        return {
            "artifact_kind": "fixed_input_current_prompt_replay",
            "source_trace_id": generation["source_trace_id"],
            "source_observation_id": generation["source_observation_id"],
            "action": generation.get("action"),
            "status": generation["status"],
            "content": generation.get("answer"),
            "replan_request": generation.get("replan_request"),
            "first_token_seconds": generation.get("first_token_seconds"),
            "generation_seconds": generation.get("generation_seconds"),
        }

    settings = Settings(_env_file=ROOT / ".env.local")
    url = urlsplit(settings.langfuse_base_url)
    if url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("只发布到本机 Langfuse")
    client = Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        base_url=settings.langfuse_base_url,
        tracing_enabled=True,
    )
    try:
        report = publish(
            client,
            items,
            results,
            reviews,
            run_version=VERSION,
            score_factory=scores,
            output_factory=output,
            metadata_overrides={
                "scope": "fixed_input_current_prompt_replay",
                "output_kind": "new_answer_or_replan_from_frozen_evidence",
                "generation_results_sha256": digest(generations),
                "publication_latency_is_not_generation_latency": "true",
            },
            description="当前合成提示词固定输入回放；只发布已生成结果，实际生成耗时见输出。补证仅记录不执行。",
        )
        (dest / "publish-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        )
        return report
    finally:
        client.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["generate", "evaluate", "publish"])
    parser.add_argument("--folder", type=Path, default=ROOT / "data/evaluation/2026-11-01")
    parser.add_argument("--families", nargs="+")
    args = parser.parse_args()
    report = (
        publish_replay(args.folder)
        if args.command == "publish"
        else asyncio.run(run(args.folder, args.command, args.families))
    )
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
