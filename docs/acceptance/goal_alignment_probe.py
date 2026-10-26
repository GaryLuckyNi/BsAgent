"""目标口径跨场景验证；默认离线，live 仅调用合成模型，不执行研究工具。"""

import argparse
import copy
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from time import perf_counter

from app.core.config import Settings
from app.graph.nodes.synthesizer import build_synthesizer_node
from app.graph.planning.request_flow import REQUEST_FLOW_VERSION
from app.graph.planning.synthesis_decision import parse_synthesis_decision
from app.graph.state import create_initial_state
from app.llm.deepseek import DeepSeekClient
from app.llm.schemas import LlmCompletionResult
from app.schemas.evidence import EvidencePackage
from langgraph.runtime import Runtime
from langgraph.store.memory import InMemoryStore

RECORD = Path(__file__).with_name("2026-10-26-goal-alignment.json")


class RecordingClient:
    def __init__(self, client, *, thinking=None):
        self.client = client
        self.thinking = thinking
        self.calls = []
        self.responses = []
        self.call_details = []

    def record_result(self, request, result):
        self.responses.append(result.content)
        self.call_details.append({
            "thinking": request.get("thinking"),
            "format_repair": json.loads(request["user_prompt"]).get("format_repair"),
            "response": result.content,
            "finish_reason": result.finish_reason,
            "usage": result.usage,
        })

    def complete_streamed_sync(self, **kwargs):
        if self.thinking is not None:
            kwargs["thinking"] = self.thinking
        self.calls.append({key: value for key, value in kwargs.items() if key != "on_delta"})
        result = self.client.complete_streamed_sync(**kwargs)
        self.record_result(kwargs, result)
        return result

    async def complete(self, **kwargs):
        self.calls.append(dict(kwargs))
        result = await self.client.complete(**kwargs)
        self.record_result(kwargs, result)
        return result


class ReplayClient:
    def __init__(self, decision):
        self.decision = decision

    async def complete(self, **_kwargs):
        return LlmCompletionResult(content=json.dumps(self.decision, ensure_ascii=False),
                                   model="recorded", provider="recorded")


def execute(case, client):
    fields = copy.deepcopy(case["state"])
    state = create_initial_state(session_id="goal-probe", tenant_id="probe", user_id="probe",
                                 user_message=fields["user_message"])
    state.update(fields)
    if isinstance(state.get("evidence"), dict):
        state["evidence"] = EvidencePackage.model_validate(state["evidence"])
    state["request_flow_version"] = REQUEST_FLOW_VERSION
    return build_synthesizer_node(llm_client=client)(state, Runtime(store=InMemoryStore()))


def accepted_decision(state):
    pending = state.get("pending_replan")
    return ({"action": "request_replan", "replan_request": pending["request"]}
            if pending else state.get("synthesis_decision") or {})


def assess(case, decision):
    if decision.get("action") != case["expected_action"]:
        return ["incorrect_action"]
    answer = decision.get("answer", "")
    errors = []
    for choices in case.get("required_any", []):
        if not any(re.search(pattern, answer) for pattern in choices):
            errors.append("missing_goal_result:" + "|".join(choices))
    for pattern in case.get("forbidden", []):
        if re.search(pattern, answer):
            errors.append("unsupported_or_scope_expanded:" + pattern)
    reasons = case.get("expected_reasons")
    if reasons and decision.get("replan_request", {}).get("reason_type") not in reasons:
        errors.append("incorrect_replan_reason")
    return errors


def validate(record, phase):
    cases = {case["case"]: case for case in record["cases"]}
    # 仅作自动粗筛；完整响应还需按目标作语义评审，不能把命中数字当作正确回答。
    for control in record.get("negative_controls", []):
        decision = {"action": "answer", "answer": control["answer"]}
        if not assess(cases[control["case"]], decision):
            raise ValueError("自动粗筛未识别已知错误回答：" + control["case"])
    rows = record["runs"][phase]
    declared_cases = record.get("phase_cases", {}).get(phase)
    expected = (set(declared_cases) if declared_cases is not None else
                {"recorded_trace"} if phase == "recorded_trace" else set(cases))
    if {row["case"] for row in rows} != expected or len(rows) != len(expected):
        raise ValueError("验证阶段缺少样本或包含重复样本")
    outcomes = []
    for row in rows:
        case = cases[row["case"]]
        state = execute(case, ReplayClient(row["decision"]))
        errors = assess(case, accepted_decision(state))
        # 推理空响应降级及协议格式修复沿用原有预算，不是新增语义审核阶段。
        if row["model_calls"] not in {1, 2, 3}:
            errors.append("model_call_budget_exceeded")
        outcomes.append({"case": row["case"], "passed": not errors, "errors": errors})
    passed = all(row["passed"] for row in outcomes)
    print(json.dumps({"phase": phase, "passed": passed, "cases": outcomes}, ensure_ascii=False))
    return passed


def run_case(case, config, *, thinking=None):
    started = perf_counter()
    client = DeepSeekClient(api_key=config.deepseek_api_key, model=config.deepseek_model,
                            base_url=config.deepseek_base_url, timeout_seconds=60)
    recorder = RecordingClient(client, thinking=thinking)
    try:
        state = execute(case, recorder)
        decision = parse_synthesis_decision(recorder.responses[-1]).model_dump(
            mode="json", exclude_none=True)
        return {"case": case["case"], "decision": decision,
                "accepted_decision": accepted_decision(state), "model_calls": len(recorder.calls),
                "model": config.deepseek_model, "input": recorder.calls[0],
                "replan_stop_reason": state.get("replan_stop_reason"),
                "seconds": round(perf_counter() - started, 3), "calls": recorder.call_details}
    finally:
        client.close()


def live(record, phase, output, *, thinking=None):
    config = Settings(_env_file=".env.local")
    rows = {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(run_case, case, config, thinking=thinking): case for case in record["cases"]}
        for future in as_completed(futures):
            case = futures[future]
            row = future.result()
            rows[case["case"]] = row
            print(json.dumps({"case": case["case"],
                              "errors": assess(case, row["accepted_decision"])},
                             ensure_ascii=False), flush=True)
            record["runs"][phase] = [rows[item["case"]] for item in record["cases"]
                                      if item["case"] in rows]
            output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    return validate(record, phase)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["validate", "live"], nargs="?", default="validate")
    parser.add_argument("--phase", default="after")
    parser.add_argument("--thinking", action="store_true", default=None,
                        help="仅诊断时覆盖提供方 thinking；默认沿用生产参数")
    parser.add_argument("--record", type=Path, default=RECORD)
    parser.add_argument("--output", type=Path, default=Path("/tmp/bsagent-goal-alignment-live.json"))
    args = parser.parse_args()
    record = json.loads(args.record.read_text())
    passed = live(record, args.phase, args.output, thinking=args.thinking) if args.mode == "live" else validate(
        record, args.phase)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
