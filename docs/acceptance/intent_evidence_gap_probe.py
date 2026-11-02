"""Router 缺口到 Planner 的隔离对照；默认离线，不执行取证工具。"""

import argparse
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from time import perf_counter

from app.core.config import Settings
from app.graph.planning.context import request_context
from app.graph.planning.planner import LlmTaskPlanner, PlannerCapabilities, PlannerOutputError
from app.graph.state import create_initial_state
from app.intent.catalog import BusinessIntentCatalog
from app.intent.router import HybridIntentRouter, IntentClassificationError
from app.intent.schemas import BusinessIntentProfile, IntentDecisionModel
from app.llm.deepseek import DeepSeekClient, LlmProviderError
from app.schemas.messages import Message
from app.tools.planning_candidates import PlanningToolCard
from planner_evidence_coverage_probe import RecordingClient
from planner_evidence_coverage_probe import assess as assess_plan

RECORD = Path(__file__).with_name("2026-10-27-intent-evidence-gap.json")


def assess(case, row):
    if row.get("error_code"):
        return ["stage_error:" + row["error_code"]]
    decision = IntentDecisionModel.model_validate(row["router"])
    errors = []
    if decision.route not in case["expected_routes"]:
        errors.append("unexpected_route")
    if bool(case.get("out_of_scope")) != (decision.intent_type is None):
        errors.append("unexpected_scope")
    if case.get("source_constraint") and decision.source_constraint != case["source_constraint"]:
        errors.append("source_constraint_changed")
    goal = decision.goal or ""
    for pattern in case.get("required_goal", []):
        if not re.search(pattern, goal):
            errors.append("goal_lost_target:" + pattern)
    gap = json.dumps(decision.available_information.get("missing_input", ""), ensure_ascii=False)
    for pattern in case.get("required_gap", []):
        if not re.search(pattern, gap):
            errors.append("missing_target_gap:" + pattern)
    if case.get("plan_checks"):
        if row.get("plan") is None:
            errors.append("missing_plan")
        else:
            errors.extend(assess_plan(case["plan_checks"], row["plan"]))
    return errors


def run_case(case, repeat, config, taxonomy):
    catalog = BusinessIntentCatalog(BusinessIntentProfile.model_validate(p) for p in taxonomy)
    client = DeepSeekClient(api_key=config.deepseek_api_key, model=config.deepseek_model,
                            base_url=config.deepseek_base_url, timeout_seconds=60)
    recorder = RecordingClient(client)
    row = {"case": case["case"], "repeat": repeat, "model": config.deepseek_model}
    started = perf_counter()
    router_calls = 0
    try:
        decision = HybridIntentRouter(catalog=catalog, llm_client=recorder).route(
            message=case["message"], context=case.get("context", {}), tenant_id="probe", user_id="probe",
        )
        row["router"] = decision.model_dump(mode="json")
        router_calls = len(recorder.calls)
        if decision.intent_type is not None and decision.route == "plan":
            history = [Message(message_id=f"history-{i}", role=message["role"], content=message["content"])
                       for i, message in enumerate(case.get("history", []))]
            state = create_initial_state(session_id="intent-gap-probe", tenant_id="probe", user_id="probe",
                                         user_message=case["message"], history=history)
            state["intent"] = row["router"]
            plan = LlmTaskPlanner(llm_client=recorder).plan(
                goal=decision.goal, intent_type=decision.intent_type, domain=decision.domain,
                subdomain=decision.subdomain, capabilities=PlannerCapabilities(tools=tuple(
                    PlanningToolCard.model_validate(card) for card in case.get("tool_candidates", [])
                )), task_results=(), remaining_iterations=2,
                planning_constraints={"user_message": case["message"], "request_context": request_context(state)},
            )
            row["plan"] = plan.model_dump(mode="json", exclude_none=True)
    except (IntentClassificationError, PlannerOutputError, LlmProviderError, ValueError) as exc:
        if "router" not in row:
            router_calls = len(recorder.calls)
        row["error_code"] = str(getattr(exc, "code", type(exc).__name__))
    finally:
        row["seconds"] = round(perf_counter() - started, 3)
        row["calls"] = [{**call, "stage": "router" if i < router_calls else "planner"}
                        for i, call in enumerate(recorder.calls)]
        client.close()
    return row


def validate(record, phase):
    cases = {case["case"]: case for case in record["cases"]}
    for row in record.get("negative_controls", []):
        if not assess(cases[row["case"]], row):
            raise ValueError("粗筛未识别负对照：" + row["case"])
    rows = record["runs"][phase]
    expected = {(c["case"], i) for c in cases.values() for i in range(c.get("repeats", 1))}
    if {(r["case"], r["repeat"]) for r in rows} != expected or len(rows) != len(expected):
        raise ValueError("样本缺失或重复")
    outcomes = [{"case": row["case"], "repeat": row["repeat"], "errors": assess(cases[row["case"]], row)}
                for row in rows]
    passed = not any(row["errors"] for row in outcomes)
    print(json.dumps({"phase": phase, "coarse_passed": passed, "results": outcomes}, ensure_ascii=False))
    return passed


def live(record, phase, output):
    config = Settings(_env_file=".env.local")
    rows = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(run_case, case, i, config, record["taxonomy"]) for case in record["cases"]
                   for i in range(case.get("repeats", 1))]
        for future in as_completed(futures):
            row = future.result()
            rows.append(row)
            print(json.dumps({"case": row["case"], "repeat": row["repeat"],
                              "seconds": row["seconds"], "error": row.get("error_code")}, ensure_ascii=False), flush=True)
            record["runs"][phase] = sorted(rows, key=lambda row: (row["case"], row["repeat"]))
            output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    return validate(record, phase)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["validate", "live"], nargs="?", default="validate")
    parser.add_argument("--phase", default="after")
    parser.add_argument("--record", type=Path, default=RECORD)
    parser.add_argument("--output", type=Path, default=Path("/tmp/bsagent-intent-evidence-gap.json"))
    args = parser.parse_args()
    record = json.loads(args.record.read_text())
    passed = live(record, args.phase, args.output) if args.mode == "live" else validate(record, args.phase)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
