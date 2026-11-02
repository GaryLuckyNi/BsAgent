"""合成核心提示词证据使用对照；固定输入，不执行生产图或业务工具。"""

from __future__ import annotations

import argparse
import ast
import asyncio
import json
import statistics
import sys
from collections import Counter
from copy import deepcopy
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import Settings

from scripts.evaluator_calibration import (
    COVERAGE_PROMPT_PATH,
    calibrated_item,
    require_coverage_judgment,
)
from scripts.offline_answer_baseline import JsonJudgeClient, judge_one, prepare_payload
from scripts.real_eval_dataset import digest
from scripts.replay_synthesis_baseline import (
    SOURCE,
    StreamingClient,
    generation_request,
    render_ast,
    replay_one,
)
from scripts.synthesis_prompt_experiment import (
    freeze_spec,
    generation_jobs,
    judge_item,
    load_cases,
    transfer_items,
)

VERSION = "synthesis-evidence-use-2026-11-01-v1"
CANDIDATE_PATH = ROOT / "docs/evaluation/synthesis-evidence-candidate-v1.md"
FAMILIES = (
    "cashflow_profit_ratios",
    "minority_interest_acquisition",
    "margin_impairment_chain",
    "low_base_vs_sequential",
    "issuance_discount",
    "annualized_profit_pe",
    "quarterly_revenue_growth",
    "unlock_quantity_type",
)


def current_core():
    literals = [
        n.value.strip()
        for n in ast.walk(render_ast())
        if isinstance(n, ast.Constant)
        and isinstance(n.value, str)
        and "你是 BsAgent 的投研报告合成器。" in n.value
    ]
    if len(literals) != 1:
        raise ValueError("当前核心提示词结构变化，必须重新核对")
    return literals[0]


def replace_core(request, core, candidate):
    if not request["system_prompt"].startswith(core + "\n"):
        raise ValueError("核心提示词前缀变化，禁止覆盖")
    result = deepcopy(request)
    result["system_prompt"] = candidate.strip() + request["system_prompt"][len(core) :]
    return result


async def run(folder, command):
    settings = Settings(_env_file=ROOT / ".env.local")
    real = [i for i in load_cases(folder) if i["metadata"]["family"] in FAMILIES]
    if len(real) != len(FAMILIES):
        raise ValueError("完整诊断样本缺失")
    cases = real + transfer_items()
    core, candidate = current_core(), CANDIDATE_PATH.read_text()
    judge_prompt = COVERAGE_PROMPT_PATH.read_text()
    base = {i["id"]: generation_request(i) for i in cases}
    output = folder / VERSION
    output.mkdir(exist_ok=True)
    spec = {
        "version": VERSION,
        "model": settings.deepseek_model,
        "source_sha256": digest(SOURCE.read_text()),
        "candidate_sha256": digest(candidate),
        "core_sha256": digest(core),
        "judge_sha256": digest(judge_prompt),
        "cases": [
            {
                "id": i["id"],
                "family": i["metadata"]["family"],
                "base_request_sha256": digest(base[i["id"]]),
                "synthetic": bool(i["metadata"].get("is_synthetic_control")),
            }
            for i in cases
        ],
        "blind_holdout": False,
        "replicates_per_arm": 1,
        "generation_order_seed": 20261101,
        "scope": "已曝光诊断题；仅改核心提示词；评审标准不传生成端",
    }
    freeze_spec(output / "spec.json", spec)
    semaphore = asyncio.Semaphore(3)
    async with httpx.AsyncClient(
        base_url=settings.deepseek_base_url, timeout=180, trust_env=False
    ) as http:
        client = StreamingClient(
            http, api_key=settings.deepseek_api_key, model=settings.deepseek_model
        )
        judge = JsonJudgeClient(
            http, api_key=settings.deepseek_api_key, model=settings.deepseek_model
        )

        async def process(item, arm):
            key = item["metadata"]["source_trace_id"]
            request = (
                base[item["id"]]
                if arm == "control"
                else replace_core(base[item["id"]], core, candidate)
            )
            fp = digest(
                {
                    "version": VERSION,
                    "arm": arm,
                    "model": settings.deepseek_model,
                    "request": request,
                }
            )
            path = output / "generations" / arm / (key + ".json")
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                g = json.loads(path.read_text())
                if g["fingerprint"] != fp:
                    raise ValueError("生成缓存变化，禁止覆盖")
            else:
                if command != "generate":
                    raise ValueError("生成结果缺失")
                async with semaphore:
                    g = await replay_one(client, request)
                g.update(
                    fingerprint=fp,
                    request=request,
                    arm=arm,
                    item_id=item["id"],
                    source_trace_id=key,
                    source_observation_id=item["metadata"]["source_observation_id"],
                    family=item["metadata"]["family"],
                    is_synthetic_control=bool(item["metadata"].get("is_synthetic_control")),
                    requested_model=settings.deepseek_model,
                )
                path.write_text(json.dumps(g, ensure_ascii=False, indent=2) + "\n")
                print(
                    json.dumps(
                        {
                            "family": g["family"],
                            "arm": arm,
                            "status": g["status"],
                            "action": g.get("action"),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            if (
                command == "evaluate"
                and not g["is_synthetic_control"]
                and g.get("action") == "answer"
            ):
                view = calibrated_item(judge_item(item, g))
                payload = prepare_payload(view)
                jfp = digest(
                    {
                        "version": VERSION,
                        "payload": payload,
                        "prompt": judge_prompt,
                        "model": settings.deepseek_model,
                    }
                )
                dest = output / "judgments" / arm / (key + ".json")
                dest.parent.mkdir(exist_ok=True, parents=True)
                if dest.exists():
                    if json.loads(dest.read_text())["fingerprint"] != jfp:
                        raise ValueError("评审缓存变化，禁止覆盖")
                else:
                    async with semaphore:
                        r = await judge_one(
                            judge, view, judge_prompt, validation_hook=require_coverage_judgment
                        )
                    r.update(
                        fingerprint=jfp,
                        arm=arm,
                        item_id=item["id"],
                        family=g["family"],
                        source_trace_id=key,
                        evaluated_answer_sha256=digest(g["answer"]),
                        evaluation_view_sha256=digest(view["expected_output"]),
                        evaluator_version="offline-answer-judge-v4",
                        prompt_sha256=digest(judge_prompt),
                        human_reviewed=False,
                    )
                    dest.write_text(json.dumps(r, ensure_ascii=False, indent=2) + "\n")
                    print(
                        json.dumps(
                            {"family": g["family"], "arm": arm, "evaluation_status": r["status"]},
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
            return g

        generations = await asyncio.gather(*(process(i, arm) for i, arm in generation_jobs(cases)))
    report = {"version": VERSION, "command": command, "arms": {}}
    for arm in ["control", "candidate"]:
        gs = [g for g in generations if g["arm"] == arm and not g["is_synthetic_control"]]
        js = [json.loads(f.read_text()) for f in (output / "judgments" / arm).glob("*.json")]
        report["arms"][arm] = {
            "real_cases": len(gs),
            "statuses": dict(Counter(g["status"] for g in gs)),
            "actions": dict(Counter(g.get("action", "error") for g in gs)),
            "judge_statuses": dict(Counter(j["status"] for j in js)),
            "generation_median_seconds": statistics.median(
                g["generation_seconds"] for g in gs if "generation_seconds" in g
            ),
            "first_token_median_seconds": statistics.median(
                g["first_token_seconds"] for g in gs if "first_token_seconds" in g
            ),
            "input_tokens": sum(g.get("usage", {}).get("input_tokens", 0) for g in gs),
            "output_tokens": sum(g.get("usage", {}).get("output_tokens", 0) for g in gs),
        }
    (output / (command + "-report.json")).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["generate", "evaluate"])
    parser.add_argument("--folder", type=Path, default=ROOT / "data/evaluation/2026-11-01")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.folder, args.command)), ensure_ascii=False))


if __name__ == "__main__":
    main()
