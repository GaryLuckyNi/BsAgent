"""通用推演约束的固定输入配对实验；不修改生产提示词或执行业务工具。"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import Settings
from app.graph.planning.synthesis_decision import decision_schema

from scripts.offline_answer_baseline import PROMPT_PATH, JsonJudgeClient, judge_one
from scripts.offline_answer_baseline import VERSION as JUDGE_VERSION
from scripts.real_eval_dataset import digest, validate_items
from scripts.replay_synthesis_baseline import (
    SOURCE,
    StreamingClient,
    generation_request,
    replay_one,
)

VERSION = "synthesis-reasoning-ab-2026-11-01-v1"
CANDIDATE_PATH = ROOT / "docs/evaluation/synthesis-reasoning-candidate-v1.md"
ARMS = ("control", "candidate")


def candidate_request(base: dict, candidate: str) -> dict:
    request = deepcopy(base)
    request["system_prompt"] += "\n\n" + candidate
    return request


def experiment_fingerprint(request, arm, model):
    return digest({"version": VERSION, "arm": arm, "model": model, "request": request})


def baseline_matches(previous, request, requested_model, source_sha):
    return previous["request"] == request and previous["fingerprint"] == digest(
        {"request": request, "model": requested_model, "source_sha": source_sha}
    )


def generation_jobs(cases):
    cases = list(cases)
    random.Random(20261101).shuffle(cases)
    return [
        (item, arm)
        for n, item in enumerate(cases)
        for arm in (ARMS if n % 2 == 0 else tuple(reversed(ARMS)))
    ]


def judge_item(item, generation):
    result = deepcopy(item)
    result["metadata"]["baseline_output"] = {"content": generation["answer"]}
    return result


def transfer_items():
    definitions = [
        (
            "bound",
            "在一个假设交易中，发行价为每股21元，约定发行价不低于参考均价的70%。仅凭这些条件能否确定参考均价就是30元？可推出什么范围？",
            "仅给定上述假设，参考均价为正数。",
            ["不能唯一确定均价；只能得出均价不超过30元"],
            {"max_average_price": "30"},
        ),
        (
            "period",
            "假设某企业上半年累计营业收入760万元，前三季度累计营业收入1240万元。第三季度单季收入是多少？这些数据能否确定第二季度单季收入？",
            "同一主体、同一年度、同一收入口径，上半年覆盖第一及第二季度，前三季度另含第三季度。",
            ["第三季度收入480万元；第二季度不能由这些数据唯一确定"],
            {"q3_revenue_wan": "480", "q2_identifiable": False},
        ),
        (
            "sum",
            "一份事项台账包含：事项A为125万元、事项B为208万元、事项C为317万元，另有事项A为125万元的重复展示。每个事项有唯一编号，同编号重复展示是同一事项。请列出参与合计的明细并求去重后的金额。",
            "事项编号A、B、C是互斥事项。重复A不代表另一批次，金额单位均为万元。",
            ["A125、B208、C317，合计650万元，不把重复A再次计入"],
            {"total_wan": "650"},
        ),
        (
            "open",
            "将以下视为已给定条件，不核实现实真伪：某企业营收同比增长12%，归母净利润同比增长8%，经营现金流/净利润为1.3且两者均为正。请仅据此初步分析经营质量，不做行业对比、定量估值或投资建议。",
            "这是开放式初步分析，不要求补齐其他指标；数据为同一主体和期间，净利润口径一致。",
            ["以已有条件给出有边界的初步研判，不因缺少估值或更多指标申请补证"],
            {"expected_action": "answer"},
        ),
    ]
    items = []
    for name, question, material, requirements, expected in definitions:
        key = "transfer_" + name
        payload = {
            "goal": question,
            "user_message": question,
            "research_materials": [{"research_goal": "按给定条件分析", "content": material}],
            "allow_replan": True,
            "required_schema": decision_schema(True),
        }
        items.append(
            {
                "id": key,
                "input": {
                    "question": question,
                    "as_of_utc": "2026-11-01T00:00:00Z",
                    "timezone": "Asia/Shanghai",
                    "synthesis_turns": [
                        {"input": {"user_prompt": payload}, "model": "synthetic_context"}
                    ],
                },
                "expected_output": {"must_include": requirements, "numeric_expected": expected},
                "metadata": {
                    "family": key,
                    "baseline_groundedness_ready": True,
                    "integrity_flags": [],
                    "source_trace_id": key,
                    "source_observation_id": key,
                    "is_synthetic_control": True,
                    "human_reviewed": False,
                    "baseline_output": {"content": ""},
                },
            }
        )
    return items


def freeze_spec(path, spec):
    if path.exists():
        if json.loads(path.read_text()) != spec:
            raise ValueError("实验条件已变化，禁止覆盖；请使用新实验版本")
    else:
        path.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n")


def load_cases(folder):
    items = json.loads((folder / "items.json").read_text())
    validate_items(items)
    return [i for i in items if i["metadata"]["baseline_groundedness_ready"]]


async def run(folder, command):
    settings = Settings(_env_file=ROOT / ".env.local")
    real = load_cases(folder)
    controls = transfer_items()
    cases = real + controls
    candidate = CANDIDATE_PATH.read_text()
    judge_prompt = PROMPT_PATH.read_text()
    output = folder / VERSION
    output.mkdir(exist_ok=True)
    bases = {i["id"]: generation_request(i) for i in cases}
    # 本轮控制组必须与上一轮已审阅的当前提示词一致，不把代码更新混入候选效果。
    for item in real:
        previous = json.loads(
            (
                folder
                / "synthesis-fixed-input-2026-11-01-v1"
                / "generations"
                / f"{item['metadata']['source_trace_id']}.json"
            ).read_text()
        )
        if not baseline_matches(
            previous, bases[item["id"]], settings.deepseek_model, digest(SOURCE.read_text())
        ):
            raise ValueError("当前基线或模型已变化，需要重新设计对照")
    spec = {
        "version": VERSION,
        "candidate_sha256": digest(candidate),
        "judge_prompt_sha256": digest(judge_prompt),
        "requested_model": settings.deepseek_model,
        "arms": list(ARMS),
        "replicates_per_case_per_arm": 1,
        "order_seed": 20261101,
        "real_cases_exposed_diagnostic": len(real),
        "synthetic_controls": len(controls),
        "blind_holdout": False,
        "thinking": False,
        "temperature": 0.2,
        "cases": [
            {
                "id": i["id"],
                "synthetic": bool(i["metadata"].get("is_synthetic_control")),
                "base_request_sha256": digest(bases[i["id"]]),
            }
            for i in cases
        ],
    }
    freeze_spec(output / "experiment-spec.json", spec)
    freeze_spec(output / "transfer-controls.json", controls)
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

        async def process(item, arm):
            meta = item["metadata"]
            key = meta["source_trace_id"]
            request = (
                bases[item["id"]]
                if arm == "control"
                else candidate_request(bases[item["id"]], candidate)
            )
            fp = experiment_fingerprint(request, arm, settings.deepseek_model)
            path = output / "generations" / arm / f"{key}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            generation = None
            if path.exists():
                generation = json.loads(path.read_text())
                if generation["fingerprint"] != fp:
                    raise ValueError("生成缓存不匹配，禁止覆盖")
                if command == "resume" and recoverable_generation(generation):
                    history = (
                        output / "attempt-history" / arm / f"{key}-{digest(generation)[:16]}.json"
                    )
                    archive_failure(history, generation)
                    generation = None
            if generation is None:
                if command not in {"generate", "resume"}:
                    raise ValueError("缺少生成结果")
                async with semaphore:
                    generation = await replay_one(generator, request)
                generation.update(
                    item_id=item["id"],
                    source_trace_id=key,
                    source_observation_id=meta["source_observation_id"],
                    family=meta["family"],
                    arm=arm,
                    fingerprint=fp,
                    experiment_version=VERSION,
                    request=request,
                    is_synthetic_control=bool(meta.get("is_synthetic_control")),
                    requested_model=settings.deepseek_model,
                    created_at=datetime.now(UTC).isoformat(),
                )
                path.write_text(json.dumps(generation, ensure_ascii=False, indent=2) + "\n")
                print(
                    json.dumps(
                        {
                            "family": meta["family"],
                            "arm": arm,
                            "status": generation["status"],
                            "action": generation.get("action"),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            if (
                command == "evaluate"
                and not meta.get("is_synthetic_control")
                and generation.get("action") == "answer"
            ):
                dest = output / "judgments" / arm / f"{key}.json"
                dest.parent.mkdir(parents=True, exist_ok=True)
                jfp = digest(
                    {
                        "answer": generation["answer"],
                        "item_sha": meta["artifact_sha256"],
                        "judge_prompt": judge_prompt,
                        "model": settings.deepseek_model,
                    }
                )
                if dest.exists():
                    if json.loads(dest.read_text())["fingerprint"] != jfp:
                        raise ValueError("评审缓存不匹配")
                else:
                    async with semaphore:
                        result = await judge_one(judge, judge_item(item, generation), judge_prompt)
                    result.update(
                        item_id=item["id"],
                        source_trace_id=key,
                        source_observation_id=meta["source_observation_id"],
                        family=meta["family"],
                        arm=arm,
                        fingerprint=jfp,
                        evaluator_version=JUDGE_VERSION,
                        prompt_sha256=digest(judge_prompt),
                        is_counterfactual_control=False,
                        human_reviewed=False,
                        evaluated_answer_sha256=digest(generation["answer"]),
                    )
                    dest.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
                    print(
                        json.dumps(
                            {
                                "family": meta["family"],
                                "arm": arm,
                                "evaluation_status": result["status"],
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
            return generation

        generations = await asyncio.gather(*(process(i, arm) for i, arm in generation_jobs(cases)))
    report = {"version": VERSION, "command": command, "arms": {}}
    for arm in ARMS:
        subset = [g for g in generations if g["arm"] == arm and not g["is_synthetic_control"]]
        report["arms"][arm] = {
            "real_cases": len(subset),
            "statuses": dict(Counter(g["status"] for g in subset)),
            "actions": dict(Counter(g.get("action", "error") for g in subset)),
            "response_models": dict(Counter(g.get("model", "unavailable") for g in subset)),
            "generation_median_seconds": statistics.median(
                g["generation_seconds"] for g in subset if "generation_seconds" in g
            ),
            "usage": {
                k: sum(g.get("usage", {}).get(k, 0) for g in subset)
                for k in ["input_tokens", "output_tokens"]
            },
        }
        if command == "evaluate":
            judgments = [
                json.loads(p.read_text()) for p in (output / "judgments" / arm).glob("*.json")
            ]
            report["arms"][arm]["judge_statuses"] = dict(Counter(j["status"] for j in judgments))
    (output / f"{command}-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    return report


def experiment_score_records(item, result, generation, arm, review=None):
    from uuid import NAMESPACE_URL, uuid5

    from scripts.replay_synthesis_baseline import replay_score_records

    if item["metadata"].get("is_synthetic_control") or generation.get("is_synthetic_control"):
        raise ValueError("合成迁移题不得发布到真实Dataset")
    if arm not in ARMS:
        raise ValueError("未知实验组")
    records = replay_score_records(item, result, generation, review)
    for record in records:
        name = record["name"].removeprefix("synthesis_replay_v1.")
        record["name"] = f"reasoning_ab_v1.{arm}.{name}"
        record["score_id"] = str(uuid5(NAMESPACE_URL, f"{VERSION}:{arm}:{item['id']}:{name}"))
        record["metadata"].update(
            experiment_version=VERSION,
            replay_version=VERSION + "-" + arm,
            arm=arm,
            scope="paired_prompt_exposed_diagnostic",
            blind_holdout=False,
            requested_model=generation.get("requested_model"),
        )
    return records


def completed_real_pairs(items, generations):
    return [
        item
        for item in items
        if not item["metadata"].get("is_synthetic_control")
        and all(generations[arm].get(item["id"], {}).get("status") == "success" for arm in ARMS)
    ]


def publish_experiment(folder, *, partial=False):
    from urllib.parse import urlsplit

    from langfuse import Langfuse
    from scripts.publish_offline_baseline import publish

    items = load_cases(folder)
    output = folder / VERSION
    spec = json.loads((output / "experiment-spec.json").read_text())
    planned_count = len(items)
    if partial:
        all_generations = {
            arm: {
                i["id"]: json.loads(
                    (
                        output / "generations" / arm / f"{i['metadata']['source_trace_id']}.json"
                    ).read_text()
                )
                for i in items
            }
            for arm in ARMS
        }
        items = completed_real_pairs(items, all_generations)
        if not items:
            raise ValueError("没有已完成的真实配对结果")
    reviews = (
        {arm: {"cases": {}} for arm in ARMS}
        if partial
        else json.loads((output / "review-overlay.json").read_text())
    )
    settings = Settings(_env_file=ROOT / ".env.local")
    url = urlsplit(settings.langfuse_base_url)
    if (
        url.scheme != "http"
        or url.hostname not in {"localhost", "127.0.0.1", "::1"}
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError("只允许发布到本机Langfuse")
    client = Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        base_url=settings.langfuse_base_url,
        tracing_enabled=True,
    )
    reports = {}
    try:
        for arm in ARMS:
            generations = {
                i["id"]: json.loads(
                    (
                        output / "generations" / arm / f"{i['metadata']['source_trace_id']}.json"
                    ).read_text()
                )
                for i in items
            }
            results = []
            for item in items:
                g = generations[item["id"]]
                if any(
                    g[k] != v
                    for k, v in {
                        "item_id": item["id"],
                        "source_trace_id": item["metadata"]["source_trace_id"],
                        "source_observation_id": item["metadata"]["source_observation_id"],
                        "arm": arm,
                    }.items()
                ):
                    raise ValueError("回放来源或实验组不一致")
                if g["fingerprint"] != experiment_fingerprint(
                    g["request"], arm, spec["requested_model"]
                ):
                    raise ValueError("回放内容与封存版本不符")
                if g.get("action") == "answer" and not partial:
                    result = json.loads(
                        (
                            output
                            / "judgments"
                            / arm
                            / f"{item['metadata']['source_trace_id']}.json"
                        ).read_text()
                    )
                else:
                    result = {
                        "item_id": item["id"],
                        "status": "not_applicable",
                        "source_trace_id": g["source_trace_id"],
                        "generation_fingerprint": g["fingerprint"],
                    }
                results.append(result)

            def scores(item, result, review, arm=arm, generations=generations):
                if partial:
                    return []  # 配额中断的快照只保存输出，不伪造质量评分。
                return experiment_score_records(item, result, generations[item["id"]], arm, review)

            def artifact(item, arm=arm, generations=generations):
                g = generations[item["id"]]
                return {
                    "artifact_kind": "paired_prompt_experiment",
                    "arm": arm,
                    "source_trace_id": g["source_trace_id"],
                    "source_observation_id": g["source_observation_id"],
                    "status": g["status"],
                    "action": g.get("action"),
                    "content": g.get("answer"),
                    "replan_request": g.get("replan_request"),
                    "requested_model": g.get("requested_model"),
                    "response_model": g.get("model"),
                    "first_token_seconds": g.get("first_token_seconds"),
                    "generation_seconds": g.get("generation_seconds"),
                    "evaluation_status": "pending_insufficient_balance"
                    if partial
                    else "model_preliminary",
                }

            reports[arm] = publish(
                client,
                items,
                results,
                reviews[arm]["cases"],
                run_version=VERSION + ("-partial-01-" if partial else "-") + arm,
                score_factory=scores,
                output_factory=artifact,
                metadata_overrides={
                    "scope": "paired_prompt_exposed_diagnostic",
                    "prompt_variant": arm,
                    "requested_model": spec["requested_model"],
                    "blind_holdout": "false",
                    "output_kind": "new_generation_from_frozen_input",
                    "experiment_spec_sha256": digest(spec),
                    "generations_sha256": digest(generations),
                    "publication_latency_is_not_generation_latency": "true",
                    "snapshot": "partial-01" if partial else "complete",
                    "planned_real_cases": str(planned_count),
                    "captured_real_pairs": str(len(items)),
                    "evaluation_pending": "true" if partial else "false",
                },
                description="通用推演约束离线配对实验；样本已曝光，仅诊断；补证不执行，合成迁移题不发布。",
            )
            (output / f"publish-report-{'partial-' if partial else ''}{arm}.json").write_text(
                json.dumps(reports[arm], ensure_ascii=False, indent=2) + "\n"
            )
        return reports
    finally:
        client.shutdown()


def recoverable_generation(generation):
    return (
        generation.get("status") == "error"
        and generation.get("error")
        in {
            "HTTPStatusError",
            "ConnectError",
            "ConnectTimeout",
            "ReadTimeout",
            "RemoteProtocolError",
        }
        and not any(a.get("raw_response") for a in generation.get("attempts", []))
    )


def archive_failure(path, generation):
    path.parent.mkdir(parents=True, exist_ok=True)
    freeze_spec(path, generation)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["generate", "resume", "evaluate", "publish"])
    parser.add_argument("--folder", type=Path, default=ROOT / "data/evaluation/2026-11-01")
    parser.add_argument(
        "--partial", action="store_true", help="只发布已完成配对输出的无评分阶段快照"
    )
    args = parser.parse_args()
    report = (
        publish_experiment(args.folder, partial=args.partial)
        if args.command == "publish"
        else asyncio.run(run(args.folder, args.command))
    )
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
