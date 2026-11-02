"""真实原生 function calling 参数回放；不执行搜索、MCP 或其他外部工具。"""

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.core.config import Settings
from app.graph.nodes.tool_agent import _tool_call_messages
from app.graph.planning.contracts import SubTaskModel
from app.graph.state import create_initial_state
from app.llm.deepseek import DeepSeekClient
from app.schemas.tools import ToolSpec
from app.tools.execution_gateway import validate_tool_arguments
from app.tools.native_calls import create_tool_call_message

ROOT = Path(__file__).resolve().parents[2]
RECORD = Path(__file__).with_name("2026-10-28-tool-call-coverage.json")


def decode(value):
    if isinstance(value, str):
        try:
            return decode(json.loads(value))
        except ValueError:
            return value
    if isinstance(value, dict):
        return {key: decode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [decode(item) for item in value]
    return value


def prepare():
    path = ROOT / "data/investigations/2026-10-28-tool-query-coverage/observations.json"
    rows = json.loads(path.read_text())["data"]
    original = next(row for row in rows if row["id"] == "8f40d8c14163bf6e")
    messages = decode(original["input"])
    payload = next(row["content"] for row in messages if row["role"] == "user")
    function = next(row["content"]["function"] for row in messages if row["role"] == "tool")
    web = {"name": function["name"], "version": "1", "description": function["description"],
           "input_schema": function["parameters"]}
    baseline = decode(original["output"])["tool_calls"][0]["args"]
    cases = [{"case": f"trace_snapshot_{i}", "payload": payload, "tool": web,
              "groups": ["指数|行情|上证|沪指", "成交额|量能", "涨跌家|涨跌分布|上涨.*下跌",
                         "领涨.*领跌|领跌.*领涨|板块涨跌|行业涨跌", "主力", "北向|北上"], "date": "2026.?10.?28", "intraday": True}
             for i in range(3)]
    partial = {**payload, "dependencies": [{"task_id": "known", "status": "success",
        "content": "已核实2026-10-28 10:55主要指数点位与涨跌幅、成交额与同比、涨跌家数和领涨领跌板块；尚缺主力与北向资金动向。"}]}
    cases.append({"case": "remaining_fund_flows", "payload": partial, "tool": web,
                  "groups": ["主力", "北向|北上"], "date": "2026.?10.?28", "intraday": True})
    other = {**payload, "user_message": "查询海辰科技2025年度三项财务数据", "request_context": {},
        "task_goal": "查询海辰科技2025年度营业收入、归母净利润及经营活动现金流净额，注明单位与报告期。",
        "completion_criteria": ["返回营业收入", "返回归母净利润", "返回经营活动现金流净额", "同一2025年度口径"],
        "dependencies": []}
    cases.append({"case": "other_three_metrics", "payload": other, "tool": web,
                  "groups": ["海辰科技", "营业收入|营收", "归母|净利润", "经营.*现金流|经营现金流"], "date": "2025"})
    tool = {"name": "mcp.fixture.statement", "version": "1", "description": "按公司和报告期查询财务数据，fields 指定返回列。",
        "input_schema": {"type": "object", "additionalProperties": False, "required": ["company_id", "period"],
            "properties": {"company_id": {"type": "string"}, "period": {"type": "string"}, "fields": {
                "type": "array", "items": {"enum": ["revenue", "net_profit", "operating_cash_flow"]}}}}}
    statement = {**other, "task_goal": other["task_goal"] + " 已确认公司代码TEST001，报告期参数20251231。"}
    cases.append({"case": "schema_field_projection", "payload": statement, "tool": tool,
                  "fields": ["revenue", "net_profit", "operating_cash_flow"], "arguments": {
                      "company_id": "TEST001", "period": "20251231"}})
    fixed = {**tool, "description": "按公司和报告期返回营业收入revenue、归母净利润net_profit、经营现金流operating_cash_flow三个固定字段。",
             "input_schema": {**tool["input_schema"], "properties": {
                 key: value for key, value in tool["input_schema"]["properties"].items() if key != "fields"}}}
    cases.append({"case": "fixed_schema_no_invented_fields", "payload": statement, "tool": fixed,
                  "arguments": {"company_id": "TEST001", "period": "20251231"}})
    ready = {**payload, "user_message": "根据已给数据计算净利率", "request_context": {},
        "task_goal": "仅依据已提供的2025年度营业收入100亿元和净利润10亿元，计算净利率；无需外部信息。",
        "completion_criteria": ["净利率=净利润/营业收入", "只使用已给2025年度同单位数据"], "dependencies": []}
    cases.append({"case": "evidence_sufficient_no_call", "payload": ready, "tool": web, "no_call": True})
    return {"trace": "e63644e40326c60cd3b2c2ab910d5ad8", "observed_bad_arguments": baseline,
            "cases": cases, "runs": {}}


def assess(case, selection):
    errors = []
    if case.get("no_call"):
        return [] if selection["selected"] is None else ["unnecessary_call"]
    if selection["selected"] != case["tool"]["name"]:
        return ["wrong_or_missing_tool"]
    args = selection["arguments"]
    if not validate_tool_arguments(tool_spec=ToolSpec.model_validate(case["tool"]), arguments=args).valid:
        errors.append("schema_invalid")
    query = args.get("query", "")
    for pattern in case.get("groups", []):
        if not re.search(pattern, query):
            errors.append("missing_query_concept:" + pattern)
    if case.get("date") and not re.search(case["date"], query):
        errors.append("time_range_lost")
    if case.get("intraday") and re.search("午间收盘|午盘收盘|午评|收盘综述", query):
        errors.append("future_session_used_as_observed")
    if case.get("fields") and set(args.get("fields", [])) != set(case["fields"]):
        errors.append("incomplete_fields")
    for key, value in case.get("arguments", {}).items():
        if args.get(key) != value:
            errors.append("wrong_argument:" + key)
    return errors


class FrozenDateTime:
    @classmethod
    def now(cls, timezone):
        return datetime.fromisoformat("2026-10-28T10:58:18+08:00").astimezone(timezone)


def generate_messages(case):
    payload = case["payload"]
    state = create_initial_state(session_id="coverage", tenant_id="test", user_id="test",
                                 user_message=payload["user_message"])
    state["request_flow_version"] = "context-routing-v1"
    state["intent"] = {"available_information": payload.get("request_context", {}).get("available_information", {})}
    state["task_results"] = payload.get("dependencies", [])
    task = SubTaskModel(id="facts", task_goal=payload["task_goal"], completion_criteria=payload["completion_criteria"],
                        depends_on=[row["task_id"] for row in state["task_results"]])
    with patch("app.graph.nodes.tool_agent.datetime", FrozenDateTime):
        return _tool_call_messages(state, task)


def run(case, messages):
    config = Settings(_env_file=str(ROOT / ".env.local"))
    client = DeepSeekClient(api_key=config.deepseek_api_key, model=config.deepseek_model,
                            base_url=config.deepseek_base_url, timeout_seconds=45)
    started = perf_counter()
    try:
        output = create_tool_call_message(llm_client=client, messages=messages,
            tool_specs=[ToolSpec.model_validate(case["tool"])], tool_choice="auto")
        selection = {"selected": output.selected_tool.name if output.selected_tool else None,
                     "arguments": output.message.tool_calls[0]["args"] if output.message.tool_calls else None,
                     "content": output.message.content}
        return {"case": case["case"], "model": config.deepseek_model, "selection": selection,
                "seconds": round(perf_counter() - started, 3), "errors": assess(case, selection),
                "messages": [{"role": message.type, "content": message.content} for message in messages]}
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["before", "after"], required=True)
    args = parser.parse_args()
    record = json.loads(RECORD.read_text()) if RECORD.exists() else prepare()
    negative = {"selected": "web_search", "arguments": record["observed_bad_arguments"]}
    assert any(error.startswith("missing_query_concept:北向") for error in assess(record["cases"][0], negative))
    prepared = [(case, generate_messages(case)) for case in record["cases"]]
    results = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(run, *item) for item in prepared]
        for future in as_completed(futures):
            row = future.result()
            results.append(row)
            print(json.dumps({key: row[key] for key in ("case", "selection", "seconds", "errors")}, ensure_ascii=False), flush=True)
    if args.phase in record["runs"]:
        record.setdefault("previous_runs", []).append({"phase": args.phase, "results": record["runs"][args.phase]})
    record["runs"][args.phase] = sorted(results, key=lambda row: row["case"])
    RECORD.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"phase": args.phase, "passed": sum(not row["errors"] for row in results), "total": len(results)}))
    if args.phase == "after":
        assert all(not row["errors"] for row in results)


if __name__ == "__main__":
    main()
