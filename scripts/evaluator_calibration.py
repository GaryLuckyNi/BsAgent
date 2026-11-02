"""新版本离线评审视图与合成校准；不覆盖原Dataset或生产提示词。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from copy import deepcopy
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import Settings

from scripts.offline_answer_baseline import JsonJudgeClient, judge_one, prepare_payload
from scripts.real_eval_dataset import digest
from scripts.synthesis_prompt_experiment import freeze_spec

VERSION = "evaluator-calibration-2026-11-01-v2"
REPAIR_VERSION = "evaluator-calibration-2026-11-01-v3"
COVERAGE_VERSION = "evaluator-calibration-2026-11-01-v4"
RUBRIC_VERSION = "bsagent-real-rubric-2026-11-01-v2"
PROMPT_PATH = ROOT / "docs/evaluation/offline-answer-judge-v2.md"
OLD_PROMPT_PATH = ROOT / "docs/evaluation/offline-answer-judge-v1.md"
REPAIR_PROMPT_PATH = ROOT / "docs/evaluation/offline-answer-judge-v3.md"
COVERAGE_PROMPT_PATH = ROOT / "docs/evaluation/offline-answer-judge-v4.md"
REQUIREMENTS = {
    "reverse_profit_base": [
        "原问要求上年同期利润基数；可信的同主体同期间直接披露值可以回答，不强制先查询本期区间。",
        "若声称反推，必须有本期绝对值与同口径同比倍率；只有增速不能唯一得到绝对基数。",
        "直接查询与条件正推应如实区分，不能声称直接披露已经完成独立反推核验。",
    ],
    "annualized_profit_pe": [
        "按用户假设计算季度利润乘4，明确是假设年化，不要求核实假设的现实真实性。",
        "当前PE需要同一时点总市值或可计算市值的有效输入，不能虚构；缺失时仍完成年化并说明核心PE尚未完成。",
    ],
    "profit_growth_forward": [
        "根据给定同比倍率与基数计算本期利润，说明单位及假设。",
        "单个增速本身不能产生区间；实际材料另有披露区间及增速下限时允许引用，不能因原问未给下限而忽略实际来源。",
    ],
    "company_basic_info": [
        "回答股票代码、名称、上市日期与退市日期；空值不编造，区分上市日期与注册日期。",
        "不评价正文没有展示任务拆分或同工具合并，执行规划不属于正文验收。",
    ],
    "quarterly_revenue_growth": [
        "环比使用同口径Q4与Q3，正负号在标题、正文和公式中一致。",
        "同比需要上年Q4单季值；固定输入无法拆分时说明同比未完成，不能用上年全年替代，同时保留能算的环比。",
        "本实验只评固定输入回答，不要求正文宣称执行额外工具调用。",
    ],
    "unlock_quantity_type": [
        "回答指定日期的解禁数量及限售类型，数量单位、计划与实际安排不混淆。",
        "结构化来源可以支持数量和类型，不普遍追加公告编号或第二来源要求；若声明去重求和，需按其统计范围正确计算。",
    ],
    "management_compensation": [
        "基于取得的管理层薪酬、持股与报告期分析，保持姓名、职务、单位对齐，区分零值与空值。",
        "业务流程的固定要求只约束确已适用的流程，不从宽泛问题扩展任意必需字段；不能用历史助手总结冒充原文已核验。",
    ],
}


def calibrated_item(item):
    result = deepcopy(item)
    family = result["metadata"]["family"]
    rubric = result["expected_output"]
    if family in REQUIREMENTS:
        rubric["must_include"] = rubric.get("must_include", [])[:3] + REQUIREMENTS[family]
    rubric["rubric_version"] = RUBRIC_VERSION
    rubric["evaluation_scope"] = "固定输入下的正文；原问优先，路径要求不自动成为正文条件"
    result["metadata"]["evaluation_view_sha256"] = digest(rubric)
    return result


def judgment_fingerprint(payload, prompt, model, *, version=VERSION):
    return digest({"version": version, "payload": payload, "prompt": prompt, "model": model})


def calibration_cases():
    definitions = [
        (
            "percent_equivalence",
            "指标甲是指标乙的240%，相当于多少倍？",
            "指标甲/指标乙=240%。",
            "相当于2.4倍。",
            {"reasoning_correctness": ["pass"]},
        ),
        (
            "bound_good",
            "价格P不低于正参考价A的75%，折价率能推出什么？",
            "P>=0.75A，A>0；折价率为1-P/A。",
            "折价率不超过25%，不能确定恰好25%。",
            {"reasoning_correctness": ["pass"]},
        ),
        (
            "bound_bad",
            "价格P不低于正参考价A的75%，折价率能推出什么？",
            "P>=0.75A，A>0；折价率为1-P/A。",
            "折价率一定恰好25%。",
            {"reasoning_correctness": ["partial", "fail"]},
        ),
        (
            "missing_pe",
            "假设季度利润20亿元，年化利润与当前PE是多少？",
            "没有总市值或总股本。",
            "按假设年化利润80亿元；缺少总市值，当前PE无法确定。",
            {"requirement_satisfaction": ["partial"], "reasoning_correctness": ["pass"]},
        ),
        (
            "structured_source",
            "查甲企业本期营业收入",
            "第三方数据接口返回同口径营业收入120000000元。",
            "本期营业收入为1.2亿元，依据所提供的数据接口记录。",
            {"factual_grounding": ["pass"], "reasoning_correctness": ["pass"]},
        ),
        (
            "sign_conflict",
            "从100下降到90的变化率是多少？",
            "上期100，本期90，口径相同。",
            "结论：增长10%。公式：(90-100)/100=-10%。",
            {"reasoning_correctness": ["partial", "fail"]},
        ),
        (
            "sum_wrong",
            "事项A125、B208、C317万元，合计多少？",
            "三事项互斥，没有重复。",
            "合计600万元。",
            {"reasoning_correctness": ["partial", "fail"]},
        ),
        (
            "causal_overclaim",
            "净利润-2、经营现金流5，能确定亏损主要原因吗？",
            "只取得两个总额，无调节表及非付现项目明细。",
            "能够确定亏损主要来自折旧摊销和减值等非付现因素。",
            {"reasoning_correctness": ["partial", "fail"]},
        ),
        (
            "cost_share",
            "收入100、成本80，材料占成本50%，材料涨价10%，其他不变，毛利率下降多少个百分点？",
            "收入、产销量不变；只调整材料采购成本。",
            "毛利率下降5个百分点。",
            {"reasoning_correctness": ["partial", "fail"]},
        ),
        (
            "direct_base",
            "本期同比预增50%—70%，对应的上期利润基数大约多少？",
            "公司上期同口径已披露利润12亿元，本期绝对金额未提供。",
            "直接披露的上期基数为12亿元。仅凭增速无法独立反推绝对金额，这里采用上期披露值。",
            {"requirement_satisfaction": ["pass"], "reasoning_correctness": ["pass"]},
        ),
        (
            "open_analysis",
            "仅按给定收入增长10%、利润增长5%初步分析，不核实现实。",
            "同口径同比数据是用户假设，无需额外指标。",
            "按给定条件，收入与利润均增长，但利润增速较慢，盈利转化效率有所减弱，具体原因尚不能唯一确定。",
            {"requirement_satisfaction": ["pass"], "factual_grounding": ["pass"]},
        ),
        (
            "period_bad",
            "上半年760、前三季度1240，Q3单季是多少？",
            "同一企业同年度同口径，上半年1—6月，前三季度1—9月。",
            "相减得480，因此Q2单季为480。",
            {"reasoning_correctness": ["partial", "fail"]},
        ),
        (
            "period_good",
            "上半年760、前三季度1240，Q3单季是多少？",
            "同一企业同年度同口径，上半年1—6月，前三季度1—9月。",
            "Q3单季为1240-760=480。",
            {"reasoning_correctness": ["pass"]},
        ),
        (
            "given_assumption",
            "仅按假设净利润为-5、收入100，计算净利率，不核验真实公司。",
            "数据是用户明确给定的假设，口径和期间一致。",
            "按假设计算净利率为-5/100=-5%。",
            {"requirement_satisfaction": ["pass"], "reasoning_correctness": ["pass"]},
        ),
    ]
    cases = []
    for key, question, material, answer, expected in definitions:
        item = {
            "id": "calibration_" + key,
            "input": {
                "question": question,
                "as_of_utc": "2026-11-01T00:00:00Z",
                "timezone": "Asia/Shanghai",
                "synthesis_turns": [
                    {
                        "input": {
                            "user_prompt": {
                                "user_message": question,
                                "research_materials": [{"content": material}],
                            }
                        }
                    }
                ],
            },
            "expected_output": {
                "must_include": ["回答原问；公式、符号与期间正确。"],
                "must_not": ["不虚构事实，不强制非必要数据。"],
            },
            "metadata": {
                "family": "calibration_" + key,
                "baseline_groundedness_ready": True,
                "integrity_flags": [],
                "baseline_output": {"content": answer},
                "source_trace_id": "synthetic_" + key,
                "source_observation_id": "synthetic_" + key,
                "is_synthetic_control": True,
                "human_reviewed": False,
            },
        }
        cases.append({"case_id": key, "item": item, "expected_verdicts": expected})
    return cases


def consistency_errors(result):
    judgment = result.get("judgment", {})
    dimensions = judgment.get("dimensions", {})
    errors = sorted(
        {
            "issue_with_nonnegative_verdict:" + issue["dimension"]
            for issue in judgment.get("issues", [])
            if dimensions.get(issue["dimension"], {}).get("verdict") in {"pass", "not_assessable"}
        }
    )
    if dimensions.get("requirement_satisfaction", {}).get("verdict") in {
        "pass",
        "not_assessable",
    } and any(
        c["status"] in {"missing", "incorrect"}
        for c in judgment.get("requirement_checks", []) or []
    ):
        errors.append("core_result_incomplete_with_nonnegative_verdict")
    return errors


def require_consistent_judgment(judgment):
    errors = consistency_errors({"judgment": judgment})
    if errors:
        raise ValueError(";".join(errors))


def require_coverage_judgment(judgment):
    if not judgment.get("requirement_checks"):
        raise ValueError("missing_requirement_checks")
    require_consistent_judgment(judgment)


async def repair_prior_judgment(
    client, item, prompt, previous, *, validation_hook=require_consistent_judgment
):
    errors = consistency_errors(previous)
    if previous.get("status") != "success" or not errors:
        raise ValueError("只能修复已记录的矛盾评审，不能重采样有效结果")
    result = await judge_one(
        client,
        item,
        prompt,
        validation_hook=validation_hook,
        repair_context={
            "previous_output": json.dumps(previous["judgment"], ensure_ascii=False),
            "validation_errors": errors,
            "representation": "validated_judgment_snapshot_not_original_raw_response",
        },
        max_attempts=1,
    )
    result["repair_of_sha256"] = digest(previous)
    return result


def assess_calibration(case, result):
    conflicts = consistency_errors(result)
    actual = {
        dim: result.get("judgment", {}).get("dimensions", {}).get(dim, {}).get("verdict")
        for dim in case["expected_verdicts"]
    }
    return {
        "case_id": case["case_id"],
        "matched": result.get("status") == "success"
        and not conflicts
        and all(actual[d] in allowed for d, allowed in case["expected_verdicts"].items()),
        "actual": actual,
        "expected_verdicts": case["expected_verdicts"],
        "evaluation_status": "inconsistent" if conflicts else result.get("status"),
        "consistency_errors": conflicts,
    }


async def calibrate(folder, *, repair_consistency=False, coverage_checks=False):
    repair_consistency = repair_consistency or coverage_checks
    run_version = (
        COVERAGE_VERSION if coverage_checks else REPAIR_VERSION if repair_consistency else VERSION
    )
    active = "v4" if coverage_checks else "v3" if repair_consistency else "v2"
    validator = (
        require_coverage_judgment
        if coverage_checks
        else require_consistent_judgment
        if repair_consistency
        else None
    )
    output = folder / run_version
    output.mkdir(exist_ok=True, parents=True)
    settings = Settings(_env_file=ROOT / ".env.local")
    cases = calibration_cases()
    prompts = (
        {active: (COVERAGE_PROMPT_PATH if coverage_checks else REPAIR_PROMPT_PATH).read_text()}
        if repair_consistency
        else {"v1": OLD_PROMPT_PATH.read_text(), "v2": PROMPT_PATH.read_text()}
    )
    prior = (
        json.loads((folder / VERSION / "v2/sum_wrong.json").read_text())
        if repair_consistency
        else None
    )
    freeze_spec(
        output / "spec.json",
        {
            "version": run_version,
            "model": settings.deepseek_model,
            "cases": cases,
            "prompts": {k: digest(v) for k, v in prompts.items()},
            "scope": "AI构造校准题；无盲测泛化声明；标准不传生成模型",
            **(
                {
                    "prior_repair_sha256": digest(prior),
                    "protocol_attempt_limit": 2,
                    "validation_protocol": "json_quote_consistency_and_core_coverage-v1"
                    if coverage_checks
                    else "json_quote_and_dimension_issue_consistency-v1",
                }
                if repair_consistency
                else {}
            ),
        },
    )
    semaphore = asyncio.Semaphore(3)
    async with httpx.AsyncClient(
        base_url=settings.deepseek_base_url, timeout=180, trust_env=False
    ) as http:
        client = JsonJudgeClient(
            http, model=settings.deepseek_model, api_key=settings.deepseek_api_key
        )

        async def process(case, arm):
            payload = prepare_payload(case["item"])
            fp = judgment_fingerprint(
                payload, prompts[arm], settings.deepseek_model, version=run_version
            )
            path = output / arm / (case["case_id"] + ".json")
            path.parent.mkdir(exist_ok=True)
            if path.exists():
                result = json.loads(path.read_text())
                if result["fingerprint"] != fp:
                    raise ValueError("校准缓存版本变化，禁止覆盖")
            else:
                async with semaphore:
                    result = await judge_one(
                        client,
                        case["item"],
                        prompts[arm],
                        validation_hook=validator,
                    )
                result.update(
                    fingerprint=fp,
                    case_id=case["case_id"],
                    evaluator_version=run_version,
                    prompt_version=arm,
                    prompt_sha256=digest(prompts[arm]),
                    human_reviewed=False,
                )
                path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            verdict = assess_calibration(case, result)
            print(json.dumps({"prompt": arm, **verdict}, ensure_ascii=False), flush=True)
            return arm, verdict

        results = await asyncio.gather(
            *(
                process(c, arm)
                for n, c in enumerate(cases)
                for arm in (tuple(prompts) if n % 2 == 0 else tuple(reversed(prompts)))
            )
        )
        prior_result = None
        if repair_consistency:
            target = next(c for c in cases if c["case_id"] == "sum_wrong")
            path = output / "prior-repair.json"
            fp = digest(
                {
                    "version": run_version,
                    "prior": prior,
                    "prompt": prompts[active],
                    "model": settings.deepseek_model,
                    "payload": prepare_payload(target["item"]),
                }
            )
            if path.exists():
                prior_result = json.loads(path.read_text())
                if prior_result["fingerprint"] != fp:
                    raise ValueError("历史修复缓存变化，禁止覆盖")
            else:
                prior_result = await repair_prior_judgment(
                    client, target["item"], prompts[active], prior, validation_hook=validator
                )
                prior_result.update(
                    fingerprint=fp, evaluator_version=run_version, human_reviewed=False
                )
                path.write_text(json.dumps(prior_result, ensure_ascii=False, indent=2) + "\n")
    report = {"version": run_version, "arms": {}}
    for arm in prompts:
        rows = [r for a, r in results if a == arm]
        report["arms"][arm] = {
            "cases": rows,
            "matched": sum(r["matched"] for r in rows),
            "statuses": dict(Counter(r["evaluation_status"] for r in rows)),
        }
    report["candidate_experiment_gate"] = all(r["matched"] for a, r in results if a == active)
    if repair_consistency:
        target = next(c for c in cases if c["case_id"] == "sum_wrong")
        report["prior_repair"] = assess_calibration(target, prior_result)
        report["candidate_experiment_gate"] &= report["prior_repair"]["matched"]
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, default=ROOT / "data/evaluation/2026-11-01")
    parser.add_argument(
        "--repair-consistency",
        action="store_true",
        help="使用v3协议验证及至多一次修复，旧产物不覆盖",
    )
    parser.add_argument(
        "--coverage-checks", action="store_true", help="使用v4逐项核心结果验收及有限协议修复"
    )
    args = parser.parse_args()
    report = asyncio.run(
        calibrate(
            args.folder,
            repair_consistency=args.repair_consistency,
            coverage_checks=args.coverage_checks,
        )
    )
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
