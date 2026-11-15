"""真实目录与模型的八条规划验收；仅 --mcp-core 执行核心样例的只读工具。"""

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
from app.graph.nodes.tool_agent import build_tool_agent_node
from app.graph.state import create_initial_state
from app.intent.catalog import BusinessIntentCatalog
from app.intent.schemas import BusinessIntentProfile
from app.llm.deepseek import DeepSeekClient
from app.operations.langfuse_observability import configure_langfuse, langfuse_agent_run
from app.skills.admin_provider import AdminSkillDefinitionProvider
from app.skills.registry import SkillRegistry
from app.tools.candidate_resolver import ToolCandidateResolver
from app.tools.market_data_config import AdminMarketDataConfigClient
from app.tools.mcp_gateway import McpGateway
from app.tools.registry import ToolRegistry
from app.tools.search import ToolSearchService
from app.tools.search.index import ToolSearchIndex

QUERY = "获取银之杰基础信息数据，包括股票代码、名称、上市日期、退市日期等"
CASES = [
    ("original", QUERY),
    ("paraphrase", "查询银之杰的证券代码、简称、上市日期和退市日期，只要基础资料。"),
    ("explicit_fields", "查询银之杰300085.SZ的股票基础资料，返回ts_code、name、list_date、delist_date、list_status。"),
    ("real_skill", "分析银之杰管理层薪酬和持股"),
    ("mixed", "获取银之杰股票代码、名称、上市日期、退市日期，并解释上市和退市日期的含义；只取基础资料，不研究行情公告。"),
    ("dependency", "先查询银之杰的股票代码，再查询该代码对应最新交易日的日线行情；股票代码必须先查得，不要推测。"),
    ("long_input", "背景：这些是与本问题无关的说明，不要求研究。" * 35 + "\n实际问题：" + QUERY),
    ("context_only", "用户已提供：某公司上市日期为2020年1月1日，退市日期为空，上市状态为L。只根据这些资料解释字段含义，不查询外部数据。"),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mcp-core", action="store_true")
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--inspect", nargs="+")
    args = parser.parse_args()
    config = Settings(_env_file=".env.local")
    folder = Path("docs/acceptance/2026-10-25-planning-tool-capability-recall")
    folder.mkdir(exist_ok=True)
    if args.inspect:
        with httpx.Client(auth=(config.langfuse_public_key, config.langfuse_secret_key),
                          timeout=20, trust_env=False) as http:
            for trace_id in args.inspect:
                response = http.get(config.langfuse_base_url + "/api/public/v2/observations", params={
                    "traceId": trace_id, "limit": 100, "fields": "core,basic,io,usage",
                    "fromStartTime": "2026-10-25T00:00:00Z",
                })
                response.raise_for_status()
                observations = response.json()["data"]
                (folder / f"observations-{trace_id}.json").write_text(
                    json.dumps(observations, ensure_ascii=False, indent=2), encoding="utf-8")
                generations = [item for item in observations if item["type"] == "GENERATION"]
                print(json.dumps({"trace_id": trace_id, "observations": len(observations),
                    "generations": len(generations),
                    "tokens": sum((item.get("usageDetails") or {}).get("total", 0) for item in generations),
                    "recall": [item.get("output") for item in observations if item["name"] == "planning_tool_recall"],
                }, ensure_ascii=False), flush=True)
        return
    run_id = uuid4().hex[:8]
    telemetry = configure_langfuse(config=config)
    client = DeepSeekClient(api_key=config.deepseek_api_key, model=config.deepseek_model,
        base_url=config.deepseek_base_url, timeout_seconds=25, persistent_sync_http=True)
    rows = []
    try:
        with httpx.Client(base_url="http://127.0.0.1:8002", timeout=15, trust_env=False) as admin:
            connection = AdminMarketDataConfigClient(base_url="http://127.0.0.1:8002", client=admin).get_active_mcp_config()
            if connection is None:
                raise RuntimeError("active_mcp_missing")
            gateway, registry = McpGateway(), ToolRegistry()
            for tool in gateway.discover_tools(connection):
                registry.register(tool, execution_binding=gateway.execution_binding(connection, tool=tool))
            snapshot = registry.snapshot()
            service = ToolSearchService(registry=snapshot, index=ToolSearchIndex.build(
                specs=snapshot.specs(), catalog_digest=snapshot.catalog_digest), candidate_k=50)
            skills = SkillRegistry(AdminSkillDefinitionProvider(client=admin))
            cards = skills.discover(domain="a_share_equity_research", subdomain="general_research")
            catalog = BusinessIntentCatalog([BusinessIntentProfile(id="probe.general",
                intent_type="financial_research", domain="a_share_equity_research",
                subdomain="general_research", description="股票投研和概念解释")])
            print(json.dumps({"catalog_tools": len(snapshot.specs()), "skills": [card.name for card in cards],
                              "model": config.deepseek_model}, ensure_ascii=False), flush=True)
            for name, question in CASES[args.start: args.start + args.limit]:
                value = create_initial_state(session_id="planning-recall-regression", tenant_id="test-regression",
                    user_id="test-regression", user_message=question, tool_catalog_digest=snapshot.catalog_digest)
                started = perf_counter()
                with langfuse_agent_run(business_trace_id=f"planning-recall-probe-{name}-{run_id}",
                    session_key=value["session_key"], tenant_id=value["tenant_id"], user_id=value["user_id"],
                    execution_segment="planning-recall-regression", input={"case": name, "question": question}) as handle:
                    with set_config_context({"callbacks": list(handle.callbacks)}) as context:
                        try:
                            context.run(build_intent_router_node(catalog=catalog, llm_client=client), value)
                            if value["intent"]["route"] == "plan":
                                context.run(build_planner_agent_node(llm_client=client, skill_registry=skills,
                                    tool_search_service=service), value)
                            row = {"case": name, "trace_id": handle.trace_id, "route": value["intent"]["route"],
                                "query": question, "candidates": (value.get("planning_tool_candidates") or {}).get("candidates", []),
                                "plan": value.get("plan"), "remote_execution": "not_run"}
                            if name == "original" and args.mcp_core and value.get("plan"):
                                tasks = value["plan"]["tasks"]
                                if len(tasks) == 1 and not tasks[0].get("skill_name"):
                                    value["active_task"] = tasks[0]
                                    update = context.run(build_tool_agent_node(registry_factory=lambda: snapshot,
                                        llm_client=client, candidate_resolver=ToolCandidateResolver(search_service=service)), value)
                                    row["remote_execution"] = {
                                        "results": update.get("task_results", []),
                                        "pending_confirmation": update.get("pending_user_input") is not None,
                                        "pending_approval": update.get("pending_tool_approval") is not None,
                                        "discovery": update.get("tool_discovery_events", {}),
                                        "calls": [{"name": record.request.tool_name, "arguments": record.request.arguments,
                                                   "status": record.result.status if record.result else "pending"}
                                                  for record in update.get("tool_calls", [])],
                                    }
                                else:
                                    row["remote_execution"] = "not_run_invalid_core_plan"
                        except Exception as exc:
                            row = {"case": name, "trace_id": handle.trace_id, "error_type": type(exc).__name__}
                    row["seconds"] = round(perf_counter() - started, 3)
                    handle.update(output=row)
                    rows.append(row)
                    (folder / f"{run_id}-{name}.json").write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
                    print(json.dumps({k: v for k, v in row.items() if k not in ("query", "candidates", "remote_execution")}, ensure_ascii=False), flush=True)
    finally:
        client.close()
        if telemetry:
            telemetry.client.flush()
            telemetry.shutdown()
    (folder / f"{run_id}-summary.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(json.dumps({"error_type": type(exc).__name__}), flush=True)
        raise SystemExit(1) from None
