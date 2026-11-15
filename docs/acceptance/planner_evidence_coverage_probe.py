"""Planner 直接证据隔离实验；默认离线，live 只调用规划模型，不执行工具。"""

import argparse
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from time import perf_counter

from app.core.config import Settings
from app.graph.planning.contracts import PlanModel, TaskResultModel
from app.graph.planning.planner import LlmTaskPlanner, PlannerCapabilities
from app.llm.deepseek import DeepSeekClient
from app.skills.definitions import SkillCard
from app.tools.planning_candidates import PlanningToolCard

RECORD = Path(__file__).with_name("2026-10-27-planner-evidence-coverage.json")


class RecordingClient:
    def __init__(self, client):
        self.client = client
        self.calls = []

    def _record(self, request, result):
        self.calls.append({
            "input": {key: value for key, value in request.items() if key != "on_delta"},
            "response": result.content, "finish_reason": result.finish_reason,
            "usage": result.usage,
        })
        return result

    def complete_streamed_sync(self, **kwargs):
        return self._record(kwargs, self.client.complete_streamed_sync(**kwargs))

    async def complete(self, **kwargs):
        return self._record(kwargs, await self.client.complete(**kwargs))


def assess(case, decision):
    errors = []
    if case.get("expected_action") and decision.get("action") != case["expected_action"]:
        errors.append("unexpected_action")
    raw_plan = decision.get("plan") if case.get("operation") == "review" else decision
    if raw_plan is None:
        return errors
    plan = PlanModel.model_validate(raw_plan)
    if "expected_tasks" in case and len(plan.tasks) != case["expected_tasks"]:
        errors.append("task_count")
    # 只检查可执行目标与验收条件，不把 public_summary 或检索词中的提及当作覆盖。
    texts = [task.task_goal + "\n" + "\n".join(task.completion_criteria) for task in plan.tasks]
    for group in case.get("required_task_groups", []):
        if not any(all(re.search(pattern, text) for pattern in group) for text in texts):
            errors.append("missing_direct_evidence:" + " & ".join(group))
    for pattern in case.get("forbidden_task_text", []):
        if any(re.search(pattern, text) for text in texts):
            errors.append("unnecessary_work:" + pattern)
    for ref, maximum in case.get("max_candidate_uses", {}).items():
        if sum(ref in task.tool_candidate_refs for task in plan.tasks) > maximum:
            errors.append("candidate_overuse:" + ref)
    if case.get("expected_skill") and [task.skill_name for task in plan.tasks] != [case["expected_skill"]]:
        errors.append("matching_skill_not_reused")
    for text in case.get("required_plan_text", []):
        if text not in json.dumps(plan.model_dump(), ensure_ascii=False):
            errors.append("missing_required_fact:" + text)
    return errors


def validate(record, phase):
    cases = {case["case"]: case for case in record["cases"]}
    for control in record.get("negative_controls", []):
        if not assess(cases[control["case"]], control["decision"]):
            raise ValueError("粗筛未识别负对照：" + control["case"])
    rows = record["runs"][phase]
    expected = {(case["case"], i) for case in record["cases"] for i in range(case.get("repeats", 1))}
    if {(r["case"], r["repeat"]) for r in rows} != expected or len(rows) != len(expected):
        raise ValueError("样本缺失或重复")
    outcomes = [{"case": row["case"], "repeat": row["repeat"],
                 "errors": assess(cases[row["case"]], row["decision"])} for row in rows]
    passed = not any(row["errors"] for row in outcomes)
    print(json.dumps({"phase": phase, "coarse_passed": passed, "results": outcomes}, ensure_ascii=False))
    return passed


def run_case(case, repeat, config):
    payload = case["input"]
    capabilities = PlannerCapabilities(
        skills=tuple(SkillCard(**{**card, "definition_hash": "fixture"}) for card in payload.get("skills", [])),
        tools=tuple(PlanningToolCard.model_validate(card) for card in payload.get("tool_candidates", [])),
    )
    client = DeepSeekClient(api_key=config.deepseek_api_key, model=config.deepseek_model,
                            base_url=config.deepseek_base_url, timeout_seconds=60)
    recorder = RecordingClient(client)
    planner = LlmTaskPlanner(llm_client=recorder)
    started = perf_counter()
    try:
        common = {
            "goal": payload["goal"], "capabilities": capabilities,
            "task_results": tuple(TaskResultModel.model_validate(row) for row in payload.get("observations", [])),
            "planning_constraints": payload.get("planning_constraints", {}),
        }
        if case.get("operation") == "review":
            result = planner.review(**common, user_message=payload["user_message"],
                                    current_plan=PlanModel.model_validate(payload["current_plan"]),
                                    remaining_revisions=2)
        else:
            result = planner.plan(**common, intent_type=payload.get("intent_type", "financial_research"),
                                  domain=payload.get("domain", "equity_research"),
                                  subdomain=payload.get("subdomain", "company_research"),
                                  remaining_iterations=payload.get("remaining_iterations", 2))
        return {"case": case["case"], "repeat": repeat, "model": config.deepseek_model,
                "decision": result.model_dump(mode="json", exclude_none=True),
                "seconds": round(perf_counter() - started, 3), "calls": recorder.calls}
    finally:
        client.close()


def live(record, phase, output):
    config = Settings(_env_file=".env.local")
    rows = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(run_case, case, i, config) for case in record["cases"]
                   for i in range(case.get("repeats", 1))]
        for future in as_completed(futures):
            row = future.result()
            rows.append(row)
            print(json.dumps({"case": row["case"], "repeat": row["repeat"],
                              "seconds": row["seconds"]}, ensure_ascii=False), flush=True)
            record["runs"][phase] = sorted(rows, key=lambda row: (row["case"], row["repeat"]))
            output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    return validate(record, phase)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["validate", "live"], nargs="?", default="validate")
    parser.add_argument("--phase", default="third_attempt")
    parser.add_argument("--record", type=Path, default=RECORD)
    parser.add_argument("--output", type=Path, default=Path("/tmp/bsagent-planner-evidence-coverage.json"))
    args = parser.parse_args()
    record = json.loads(args.record.read_text())
    passed = live(record, args.phase, args.output) if args.mode == "live" else validate(record, args.phase)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
