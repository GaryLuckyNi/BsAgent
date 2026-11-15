"""缺失必要事实的入口路由验证；默认离线校验，live 仅调用 Router 模型。"""

import argparse
import json
from pathlib import Path

from app.core.config import Settings
from app.graph.runtime import _route_after_intent
from app.intent.catalog import BusinessIntentCatalog
from app.intent.router import HybridIntentRouter
from app.intent.schemas import BusinessIntentProfile
from app.llm.deepseek import DeepSeekClient
from app.llm.schemas import LlmCompletionResult

RECORD = Path(__file__).with_name("2026-10-26-intent-evidence-routing.json")


class ReplayClient:
    def __init__(self, decision):
        self.decision = decision
        self.calls = 0

    async def complete(self, **_kwargs):
        self.calls += 1
        return LlmCompletionResult(content=json.dumps(self.decision, ensure_ascii=False),
                                   model="recorded", provider="recorded")


def route(case, client):
    payload = case["input"]
    catalog = BusinessIntentCatalog(BusinessIntentProfile.model_validate(profile)
                                    for profile in payload["allowed_taxonomy"])
    return HybridIntentRouter(catalog=catalog, llm_client=client).route(
        message=payload["message"], context=payload["context"], tenant_id="probe", user_id="probe")


def validate(record, phase):
    results = {row["case"]: row for row in record["runs"][phase]}
    errors = []
    for case in record["cases"]:
        client = ReplayClient(results[case["case"]]["decision"])
        decision = route(case, client)
        expected_nodes = {"plan": "planner_agent", "direct_answer": "verifier", "synthesize": "synthesizer"}
        node = _route_after_intent({"intent": decision.model_dump()})
        if decision.route not in case["expected_routes"] or node not in {
            expected_nodes[expected] for expected in case["expected_routes"]
        }:
            errors.append(case["case"] + ": incorrect_route")
        if client.calls != 1:
            errors.append(case["case"] + ": extra_model_call")
    print(json.dumps({"phase": phase, "passed": not errors, "errors": errors}, ensure_ascii=False))
    return not errors


def live(record):
    config = Settings(_env_file=".env.local")
    client = DeepSeekClient(api_key=config.deepseek_api_key, model=config.deepseek_model,
                            base_url=config.deepseek_base_url, timeout_seconds=45)
    results = [{"case": case["case"], "decision": route(case, client).model_dump(mode="json")}
               for case in record["cases"]]
    record["runs"]["live"] = results
    output = Path("/tmp/bsagent-intent-evidence-live.json")
    output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    print(str(output))
    return validate(record, "live")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["validate", "live"], nargs="?", default="validate")
    parser.add_argument("--phase", default="after")
    args = parser.parse_args()
    record = json.loads(RECORD.read_text())
    passed = live(record) if args.mode == "live" else validate(record, args.phase)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
