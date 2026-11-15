"""对相同查询比较直连与旧模式；只记录公开来源和有界摘要，不输出凭证。"""

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from time import perf_counter
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.core.config import Settings
from app.search.base import WebSearchProviderError
from app.search.deepseek import DeepSeekWebSearchClient
from app.search.deepseek_api import DeepSeekSearchApiClient
from app.search.schemas import SearchResearchContext

CASES = [
    ("entity", "长鑫存储 上市主体 股票代码 招股书", "核实长鑫存储的上市主体和证券代码"),
    ("news", "2026年10月23日 A股 收盘 市场 涨跌原因", "查询2026年10月23日A股市场涨跌原因"),
    (
        "valuation",
        "贵州茅台 2026年10月23日 市盈率 市净率",
        "查询贵州茅台2026年10月23日的市盈率和市净率",
    ),
]


async def probe(mode, config):
    rows = []
    cls = DeepSeekSearchApiClient if mode == "direct" else DeepSeekWebSearchClient
    client = cls(
        api_key=config.deepseek_api_key,
        timeout_seconds=config.web_search_timeout_seconds,
        **(
            {"base_url": config.web_search_base_url}
            if mode == "direct"
            else {
                "base_url": config.deepseek_base_url,
                "model": config.deepseek_model,
            }
        ),
    )
    for name, query, question in CASES:
        started = perf_counter()
        try:
            result = await client.search(
                query=query,
                max_results=3,
                research_context=(
                    SearchResearchContext(
                        task_goal=question,
                        completion_criteria=(question,),
                        as_of=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
                        original_question=question,
                    )
                ),
            )
            row = {
                "case": name,
                "mode": mode,
                "status": "success",
                "query": result.query,
                "duration_ms": round((perf_counter() - started) * 1000),
                "answer_chars": len(result.answer),
                "coverage": result.coverage.model_dump() if result.coverage else None,
                "usage": result.usage,
                "sources": [
                    {
                        "title": item.title,
                        "url": str(item.url),
                        "snippet": item.snippet[:240],
                        "published_at": item.published_at,
                    }
                    for item in result.results
                ],
            }
        except WebSearchProviderError as exc:
            row = {
                "case": name,
                "mode": mode,
                "status": "failed",
                "error_code": exc.code,
                "http_status": exc.status_code,
                "retryable": exc.retryable,
                "duration_ms": round((perf_counter() - started) * 1000),
            }
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    return rows


def agent_probe(config):
    """真实工具选择、Gateway、Search 与合成节点；隔离于生产会话和外部 MCP。"""
    from app.graph.nodes.synthesizer import build_synthesizer_node
    from app.graph.nodes.tool_agent import build_tool_agent_node
    from app.graph.state import create_initial_state
    from app.llm.deepseek import DeepSeekClient
    from app.tools.candidate_resolver import ToolCandidateResolver
    from app.tools.registry import ToolRegistry
    from app.tools.search import ToolSearchIndex, ToolSearchService
    from app.tools.web_search import WebSearchTool

    question = "请联网搜索贵州茅台2026年10月23日的市盈率和市净率，说明资料来源、时间和局限。"
    registry = ToolRegistry()
    registry.register(
        WebSearchTool(
            client=DeepSeekSearchApiClient(
                api_key=config.deepseek_api_key,
                base_url=config.web_search_base_url,
                timeout_seconds=config.web_search_timeout_seconds,
            )
        )
    )
    snapshot = registry.snapshot()
    service = ToolSearchService(
        registry=snapshot,
        candidate_k=20,
        index=ToolSearchIndex.build(
            specs=snapshot.specs(),
            catalog_digest=snapshot.catalog_digest,
        ),
    )
    client = DeepSeekClient(
        api_key=config.deepseek_api_key,
        model=config.deepseek_model,
        base_url=config.deepseek_base_url,
        timeout_seconds=config.llm_timeout_seconds,
    )
    state = create_initial_state(
        session_id="search-api-isolated-probe",
        tenant_id="probe",
        user_id="probe",
        user_message=question,
        permission_scopes=["web:search"],
        tool_catalog_digest=snapshot.catalog_digest,
    )
    # 不使用生产持久化恢复上下文，本探针只核验隔离的节点衔接。
    state["recovery_policy_version"] = None
    task = {
        "id": "facts",
        "task_goal": question,
        "completion_criteria": ["提供指定日期的市盈率和市净率，保留来源与局限"],
    }
    state["active_task"] = task
    state["plan"] = {"tasks": [task]}
    state["intent"] = {"intent_type": "financial_research", "goal": question}
    started = perf_counter()
    try:
        update = build_tool_agent_node(
            registry_factory=lambda: snapshot,
            llm_client=client,
            candidate_resolver=ToolCandidateResolver(search_service=service),
        )(state)
        state.update(update)
        records = state["tool_calls"]
        if len(records) != 1 or records[0].request.tool_name != "web_search":
            raise RuntimeError("真实执行模型未生成单次 web_search 调用")
        record = records[0]
        if record.result is None or record.result.status != "success":
            raise RuntimeError("真实搜索执行未成功")
        result = record.result.result
        assert result["answer"] == "" and result["coverage"]["status"] == "unknown"
        assert any(item["snippet"] for item in result["results"])
        build_synthesizer_node(llm_client=client)(state)
        draft = (state.get("draft_answer") or {}).get("content", "")
        if not draft:
            raise RuntimeError("未生成合成答复")
        row = {
            "case": "isolated_agent_and_synthesis",
            "mode": "direct",
            "status": "success",
            "duration_ms": round((perf_counter() - started) * 1000),
            "arguments": record.request.arguments,
            "result_count": len(result["results"]),
            "coverage": result["coverage"],
            "synthesis_answer": draft,
            "sources": result["results"],
        }
        print(json.dumps(row, ensure_ascii=False), flush=True)
        return [row]
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("direct", "legacy", "both"), default="direct")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--agent", action="store_true")
    args = parser.parse_args()
    config = Settings(_env_file=Path(__file__).resolve().parents[2] / ".env.local")
    if not config.deepseek_api_key:
        print("未配置 DeepSeek API Key，未运行真实接口探针。", file=sys.stderr)
        return 2
    rows = agent_probe(config) if args.agent else []
    if not args.agent:
        for mode in ("direct", "legacy") if args.mode == "both" else (args.mode,):
            rows.extend(asyncio.run(probe(mode, config)))
    if args.output:
        args.output.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return 0 if all(row["status"] == "success" for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
