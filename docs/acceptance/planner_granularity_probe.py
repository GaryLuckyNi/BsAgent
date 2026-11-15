"""颗粒度隔离验证：默认校验已有记录；live 模式只调用模型，不派发工具。"""

import argparse
import asyncio
import json
from pathlib import Path

from app.core.config import Settings
from app.graph.planning.contracts import PlanModel
from app.graph.planning.planner import _SYSTEM_PROMPT
from app.llm.deepseek import DeepSeekClient

RECORD = Path(__file__).with_name("2026-10-26-planner-granularity.json")


def validate(record, phase):
    results = {row["case"]: row for row in record["runs"][phase]}
    errors = []
    for case in record["cases"]:
        plan = PlanModel.model_validate(results[case["case"]]["plan"])
        if "expected_tasks" in case and len(plan.tasks) != case["expected_tasks"]:
            errors.append(case["case"] + ": task_count")
        for ref, maximum in case.get("max_candidate_uses", {}).items():
            if sum(ref in task.tool_candidate_refs for task in plan.tasks) > maximum:
                errors.append(case["case"] + ": repeated_main_quote_candidate")
        for text in case.get("required_plan_text", []):
            if text not in json.dumps(plan.model_dump(), ensure_ascii=False):
                errors.append(case["case"] + ": missing_required_fact")
    print(json.dumps({"phase": phase, "passed": not errors, "errors": errors}, ensure_ascii=False))
    return not errors


async def live(record):
    config = Settings(_env_file=".env.local")
    client = DeepSeekClient(api_key=config.deepseek_api_key, model=config.deepseek_model,
                            base_url=config.deepseek_base_url, timeout_seconds=45)
    results = []
    for case in record["cases"]:
        response = await client.complete(system_prompt=_SYSTEM_PROMPT,
            user_prompt=json.dumps(case["input"], ensure_ascii=False), temperature=0,
            max_tokens=2048, thinking=False)
        plan = PlanModel.model_validate_json(response.content)
        results.append({"case": case["case"], "model": response.model, "usage": response.usage,
                        "task_count": len(plan.tasks), "plan": plan.model_dump(mode="json")})
    record["runs"]["live"] = results
    output = Path("/tmp/bsagent-planner-granularity-live.json")
    output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    print(str(output))
    return validate(record, "live")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["validate", "live"], nargs="?", default="validate")
    parser.add_argument("--phase", default="final")
    args = parser.parse_args()
    record = json.loads(RECORD.read_text())
    passed = asyncio.run(live(record)) if args.mode == "live" else validate(record, args.phase)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
