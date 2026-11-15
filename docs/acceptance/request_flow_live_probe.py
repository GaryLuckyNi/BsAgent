"""用真实模型验证已修改节点；材料为合成样本，不调用外部 MCP 或搜索。"""

import argparse
import json
import sys
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import httpx
from langchain_core.runnables.config import set_config_context

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.core.config import Settings
from app.graph.nodes.intent_router import build_intent_router_node
from app.graph.nodes.planner_agent import build_planner_agent_node
from app.graph.nodes.synthesizer import build_synthesizer_node
from app.graph.state import create_initial_state
from app.intent.catalog import BusinessIntentCatalog
from app.intent.schemas import BusinessIntentProfile
from app.llm.deepseek import DeepSeekClient
from app.operations.langfuse_observability import configure_langfuse, langfuse_agent_run


CASES = [
    ("router_explain", "router", "什么是市盈率？用两句话解释，不需要查数据。", None, "direct_answer"),
    ("router_mixed", "router", "解释市盈率，并查询虚构甲公司今天的股价计算最新市盈率。", None, "plan"),
    ("missing_task", "synthesis", "必须给出虚构甲公司2025年市盈率并与同行比较；缺少必要数据时安排补证。",
     "财报已查完：2025年每股收益2元，未获取股价及同行估值。", "request_replan"),
    ("new_evidence", "synthesis", "判断虚构甲公司利润增长是否可持续。出现重大异常项目时必须核查对应公告。",
     "已查财报：净利润20亿元，其中18亿元为新出现的资产处置收益，财报未说明交易性质，需要查处置公告。", "request_replan"),
    ("evidence_conflict", "synthesis", "核实虚构甲公司2025年准确收入后解释增长，不要在冲突数据中随意选一个。",
     "两份同日、同口径且均注明最终审计版的报表分别称收入13亿元与16亿元，未找到重述或更正说明。", "request_replan"),
    ("path_invalid", "synthesis", "比较虚构行业三家公司2025年收入，必须取得三家数据，用户已允许从交易所公开文件取证。",
     "行业汇总接口已确认永久下线；没有可直接替代的接口。可行路径是先取公告目录，再逐家下载报表取数，原计划只有一次汇总查询。", "request_replan"),
    ("premise_invalid", "synthesis", "按虚构甲公司当前主营业务选择同行比较估值，必须核实可比对象。",
     "已查公司报告：原半导体业务已整体出售，现主营医疗器械。原计划只安排核实半导体同业估值，尚无医疗同业资料。", "request_replan"),
    ("permission_only", "synthesis", "根据目前已有材料说明风险；如果数据源没权限，可说明限制，不必换研究方向。",
     "财报查询因账号权限不足失败，原任务已包含所需财报查询。已有用户材料说明收入承压。", "answer"),
    ("timeout_only", "synthesis", "根据目前已有材料说明风险，行情暂时无法取得时如实保留限制。",
     "行情查询临时超时，原任务已包含行情取证；用户提供的财报显示经营现金流下滑。", "answer"),
    ("partial_only", "synthesis", "只按用户提供数据作初步解释，不要求最新行情或完整报告。",
     "用户已提供收入同比增长10%，但没有现金流与股价，不要求进一步核实。", "answer"),
    ("finished", "synthesis", "只概括虚构公司的收入及风险，使用给定资料即可。",
     "2025年收入100亿元、同比增长10%；经营现金流20亿元，无其他需核实事项。", "answer"),
    ("router_reuse", "router_plan", "用户已提供甲公司2025年每股收益为2元，沿用这个已知前提，不必再次查询每股收益。只查询最近交易日股价，再按2025年每股收益计算静态市盈率。", None, "plan"),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--limit", type=int, default=len(CASES))
    parser.add_argument("--inspect", nargs="+")
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    config = Settings(_env_file=".env.local")
    if args.inspect:
        with httpx.Client(auth=(config.langfuse_public_key, config.langfuse_secret_key),
                          timeout=15, trust_env=False) as http:
            for trace_id in args.inspect:
                response = http.get(config.langfuse_base_url + "/api/public/v2/observations", params={
                    "traceId": trace_id, "limit": 100, "fields": "core,basic,io,usage",
                    "fromStartTime": "2026-10-24T00:00:00Z", "toStartTime": "2026-10-26T00:00:00Z",
                })
                response.raise_for_status()
                rows = response.json()["data"]
                observations = []
                for item in rows:
                    def decode(value):
                        if isinstance(value, str):
                            try:
                                return decode(json.loads(value))
                            except ValueError:
                                return value
                        if isinstance(value, dict):
                            return {key: decode(val) for key, val in value.items()}
                        return value
                    inputs = decode(item.get("input"))
                    outputs = decode(item.get("output"))
                    observations.append({
                        "name": item["name"], "type": item["type"],
                        "output": outputs, "usage": item.get("usageDetails"),
                        "input_keys": list(inputs) if isinstance(inputs, dict) else [],
                    })
                if args.summary:
                    generations = [row for row in observations if row["type"] == "GENERATION"]
                    print(json.dumps({
                        "trace_id": trace_id, "generations": len(generations),
                        "names": [row["name"] for row in generations],
                        "tokens": sum((row["usage"] or {}).get("total", 0) for row in generations),
                        "root_output": next((row["output"] for row in observations if row["name"] == "bs-agent.run"), None),
                    }, ensure_ascii=False), flush=True)
                    continue
                print(json.dumps({"trace_id": trace_id, "observations": observations}, ensure_ascii=False), flush=True)
        return
    telemetry = configure_langfuse(config=config)
    client = DeepSeekClient(
        api_key=config.deepseek_api_key, model=config.deepseek_model,
        base_url=config.deepseek_base_url, timeout_seconds=25, persistent_sync_http=True,
    )
    catalog = BusinessIntentCatalog([BusinessIntentProfile(
        id="regression.general", intent_type="financial_research", domain="a_share_equity_research",
        subdomain="general_research", description="股票投研与金融概念解释",
    )])
    try:
        for name, kind, question, material, expected in CASES[args.start:args.start + args.limit]:
            value = create_initial_state(
                session_id="routing-regression", tenant_id="test-regression", user_id="test-regression",
                user_message=question,
            )
            business_id = f"request-flow-probe-{name}-{uuid4().hex}"
            started = perf_counter()
            with langfuse_agent_run(
                business_trace_id=business_id, session_key=value["session_key"],
                tenant_id=value["tenant_id"], user_id=value["user_id"],
                execution_segment="request-flow-regression", input={"case": name, "question": question},
            ) as handle:
                with set_config_context({"callbacks": list(handle.callbacks)}) as model_context:
                    if kind.startswith("router"):
                        model_context.run(build_intent_router_node(catalog=catalog, llm_client=client), value)
                        action = value["intent"]["route"]
                        reason = None
                        if kind == "router_plan" and action == "plan":
                            model_context.run(build_planner_agent_node(llm_client=client), value)
                    else:
                        value.update(
                            request_flow_version="context-routing-v1", planner_iteration=1,
                            intent={"intent_type": "financial_research", "goal": question},
                            plan={"tasks": [{"id": "facts", "task_goal": "查询公司财报与相关数据"}]},
                            task_results=[{"task_id": "facts", "status": "success", "content": material}],
                        )
                        # 合成器走原生流式提供方，已自行记录 Generation；
                        # 不注入缺少 LangGraph runtime 的 LangChain 回调上下文。
                        build_synthesizer_node(llm_client=client)(value)
                        action = "request_replan" if value.get("pending_replan") else "answer"
                        reason = (value.get("pending_replan") or {}).get("request", {}).get("reason_type")
                row = {
                    "case": name, "model": config.deepseek_model, "expected": expected,
                    "actual": action, "match": action == expected, "reason_type": reason,
                    "trace_id": handle.trace_id, "seconds": round(perf_counter() - started, 3),
                    "stop_reason": value.get("replan_stop_reason"),
                }
                if kind == "router_plan":
                    row["available_information"] = value["intent"]["available_information"]
                    row["plan"] = value.get("plan")
                handle.update(output=row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
    finally:
        client.close()
        if telemetry:
            telemetry.client.flush()
            telemetry.shutdown()


if __name__ == "__main__":
    main()
