import asyncio
import json
import re

from app.graph.planning.json_output import extract_json_object
from app.graph.planning.templates import build_risk_controls
from app.graph.state import AgentState
from app.llm.factory import LlmClient
from app.schemas.tools import ToolCallRequest, ToolSpec
from app.skills.configured import RESOLVED_SYMBOL_PLACEHOLDER, extract_company_query


_EQUITY_DIMENSIONS = {
    "business",
    "market",
    "valuation",
    "financial",
    "risk",
    "technical",
}

_DEFAULT_EQUITY_DIMENSIONS = [
    "business",
    "market",
    "valuation",
    "financial",
    "risk",
]


def has_stock_tooling(tool_specs: dict[str, ToolSpec]) -> bool:
    return any(name.endswith(".stock.resolve_company") for name in tool_specs)


def build_equity_research_evidence_plan(
    *,
    state: AgentState,
    intent: dict,
    llm_client: LlmClient | None,
    tool_specs: dict[str, ToolSpec],
) -> dict:
    task = _plan_equity_research_task(
        state=state,
        intent=intent,
        llm_client=llm_client,
    )
    requested_tools = _equity_research_tool_requests(
        user_message=state["user_message"],
        tenant_id=state["tenant_id"],
        user_id=state["user_id"],
        task=task,
        tool_specs=tool_specs,
        risk_level=intent.get("risk_level", "medium"),
    )
    selected_tool_names = [request.tool_name for request in requested_tools]
    has_rag = any(request.tool_name == "rag_search" for request in requested_tools)
    return {
        "objective": "先理解用户投研目标，再按需获取 MCP 与 RAG 证据并生成回答",
        "execution_mode": "tool_execution",
        "selected_skill": {
            "name": intent.get("candidate_skill") or "equity_research",
            "selection_reason": "equity_research_dynamic_evidence_plan",
        },
        "analysis_dimensions": task["analysis_dimensions"],
        "answer_depth": task["answer_depth"],
        "execution_steps": [
            "understand_equity_research_intent",
            "build_market_data_evidence_plan",
            "execute_mcp_tools",
            *(["retrieve_evidence"] if has_rag else []),
            "analyze_evidence_and_tool_results",
            "synthesize_answer",
            "verify_answer",
        ],
        "retrieval": {
            "required": has_rag,
            "optional": has_rag,
            "strategy": "rag_search" if has_rag else "none",
            "evidence_policy": "answer_prefers_cited_evidence",
            "failure_policy": "continue_with_structured_evidence"
            if has_rag
            else "none",
        },
        "tool_policy": {
            "allowed_tools": selected_tool_names,
            "approval_required": any(
                tool_specs[request.tool_name].risk_level == "high"
                for request in requested_tools
                if request.tool_name in tool_specs
            ) or intent.get("risk_level") == "high",
        },
        "response_policy": {
            "format": "equity_research_answer",
            "response_type": "equity_research_report",
            "citation_required": False,
            "uncertainty": "explicit",
            "must_reference_user_message": True,
            "disclaimer_required": True,
            "use_llm": True,
        },
        "risk_controls": build_risk_controls(intent),
        "tool_selection": {
            "source": "planner_dynamic_equity_research",
            "candidate_tool": None,
            "selected_tool": None,
            "selected_tools": selected_tool_names,
            "available_tool_count": len(tool_specs),
            "decision": "planner_emitted_mcp_and_rag_evidence_requests",
            "reason_codes": [
                *intent.get("reason_codes", []),
                "equity_research_task_decomposed",
            ],
        },
        "requested_tools": requested_tools,
    }


def _plan_equity_research_task(
    *,
    state: AgentState,
    intent: dict,
    llm_client: LlmClient | None,
) -> dict:
    fallback = {
        "company_query": extract_company_query(state["user_message"]),
        "analysis_dimensions": _heuristic_equity_dimensions(state["user_message"]),
        "need_mcp": True,
        "need_rag": False,
        "answer_depth": "standard",
    }
    if _is_standard_company_snapshot(
        intent=intent,
        user_message=state["user_message"],
        analysis_dimensions=fallback["analysis_dimensions"],
    ):
        return fallback
    if llm_client is None:
        return fallback
    try:
        result = asyncio.run(
            llm_client.complete(
                system_prompt=(
                    "你是 BsAgent 的 A 股投研任务拆解器。只能输出 JSON object，不要解释。"
                    "先判断用户要分析的公司和维度，再决定是否需要 MCP 市场数据和 RAG 证据。"
                    "普通公司综合分析、行情、估值、财务和技术指标应由 MCP 结构化数据完成，"
                    "need_rag=false。只有用户明确要求研报、公告原文、制度文档、知识库资料或"
                    "需要非结构化背景证据时，need_rag 才能为 true。"
                    "analysis_dimensions 只能从 business、market、valuation、financial、risk、technical 中选择。"
                    "宽泛的“分析一下某公司”应覆盖 business、market、valuation、financial、risk。"
                    "不得提及任何模型厂商。"
                ),
                user_prompt=json.dumps(
                    {
                        "user_message": state["user_message"],
                        "intent": intent,
                        "required_schema": {
                            "company_query": "string",
                            "analysis_dimensions": "string[]",
                            "need_mcp": "boolean",
                            "need_rag": "boolean",
                            "answer_depth": "brief|standard|deep",
                        },
                    },
                    ensure_ascii=False,
                ),
                temperature=0.0,
                max_tokens=512,
                thinking=False,
            )
        )
        payload = extract_json_object(result.content)
    except Exception:
        return fallback
    return {
        "company_query": _safe_company_query(
            payload.get("company_query"),
            fallback=fallback["company_query"],
        ),
        "analysis_dimensions": _safe_equity_dimensions(
            payload.get("analysis_dimensions"),
            fallback=fallback["analysis_dimensions"],
        ),
        "need_mcp": payload.get("need_mcp") is not False,
        "need_rag": payload.get("need_rag") is not False,
        "answer_depth": (
            payload.get("answer_depth")
            if payload.get("answer_depth") in {"brief", "standard", "deep"}
            else fallback["answer_depth"]
        ),
    }


def _is_standard_company_snapshot(
    *,
    intent: dict,
    user_message: str,
    analysis_dimensions: list[str],
) -> bool:
    return (
        intent.get("sub_intent") == "company_snapshot"
        and analysis_dimensions == _DEFAULT_EQUITY_DIMENSIONS
        and not any(
            marker in user_message
            for marker in ("研报", "公告原文", "知识库", "制度文档")
        )
    )


def _safe_company_query(value: object, *, fallback: str) -> str:
    if isinstance(value, str) and value.strip():
        return extract_company_query(value)
    return fallback


def _safe_equity_dimensions(value: object, *, fallback: list[str]) -> list[str]:
    if not isinstance(value, list):
        return fallback
    selected = [
        item
        for item in value
        if isinstance(item, str) and item in _EQUITY_DIMENSIONS
    ]
    return list(dict.fromkeys(selected)) or fallback


def _heuristic_equity_dimensions(user_message: str) -> list[str]:
    dimensions = []
    if any(marker in user_message for marker in ("技术", "K线", "k线", "走势", "均线")):
        dimensions.append("technical")
    if any(marker in user_message for marker in ("估值", "PE", "PB", "市盈率", "市净率")):
        dimensions.append("valuation")
    if any(marker in user_message for marker in ("财务", "财报", "年报", "利润", "现金流", "毛利率", "ROE")):
        dimensions.append("financial")
    if any(marker in user_message for marker in ("风险", "问询", "处罚", "诉讼", "减持", "质押")):
        dimensions.append("risk")
    if any(marker in user_message for marker in ("业务", "主营", "行业", "基本面")):
        dimensions.append("business")
    if any(marker in user_message for marker in ("行情", "股价", "涨跌", "成交", "换手")):
        dimensions.append("market")
    return list(dict.fromkeys(dimensions)) or list(_DEFAULT_EQUITY_DIMENSIONS)


def _equity_research_tool_requests(
    *,
    user_message: str,
    tenant_id: str,
    user_id: str,
    task: dict,
    tool_specs: dict[str, ToolSpec],
    risk_level: str,
) -> list[ToolCallRequest]:
    dimensions = set(task["analysis_dimensions"])
    requests: list[ToolCallRequest] = []
    resolver = _tool_by_suffix(tool_specs, ".stock.resolve_company")
    if resolver is not None:
        requests.append(
            _tool_request(
                tool=resolver,
                arguments={
                    "query": task["company_query"],
                    "required_fields": ["symbol", "name"],
                    "optional_fields": ["exchange"],
                },
                purpose="解析用户提到的 A 股公司并标准化证券代码",
                approval_required=risk_level == "high" or resolver.risk_level == "high",
            )
        )
    if task["need_mcp"]:
        profile = _tool_by_suffix(tool_specs, ".stock.company_profile")
        if profile is not None and ("business" in dimensions or "financial" in dimensions):
            requests.append(
                _tool_request(
                    tool=profile,
                    arguments={
                        "symbol": RESOLVED_SYMBOL_PLACEHOLDER,
                        "required_fields": ["name"],
                        "optional_fields": [
                            "industry",
                            "main_business",
                            "board",
                            "registered_address",
                        ],
                    },
                    purpose="获取公司业务画像与行业基础信息",
                    approval_required=risk_level == "high" or profile.risk_level == "high",
                )
            )
        price_trend = _tool_by_suffix(tool_specs, ".stock.price_trend")
        if price_trend is not None and dimensions & {"market", "technical"}:
            requests.append(
                _tool_request(
                    tool=price_trend,
                    arguments={
                        "symbol": RESOLVED_SYMBOL_PLACEHOLDER,
                        "window_days": 120,
                        "required_fields": ["latest_price"],
                        "optional_fields": [
                            "return_20d",
                            "return_60d",
                            "ma20",
                            "ma60",
                            "volatility_20d",
                            "max_drawdown",
                        ],
                    },
                    purpose="获取中短期价格趋势、波动率与回撤指标",
                    approval_required=(
                        risk_level == "high" or price_trend.risk_level == "high"
                    ),
                )
            )
        market = _tool_by_suffix(tool_specs, ".stock.market_snapshot")
        if market is not None and dimensions & {"market", "valuation", "technical"}:
            requests.append(
                _tool_request(
                    tool=market,
                    arguments={
                        "symbol": RESOLVED_SYMBOL_PLACEHOLDER,
                        "required_fields": ["price"],
                        "optional_fields": [
                            "pct_change",
                            "turnover",
                            "market_cap",
                            "pe_ttm",
                            "pb",
                        ],
                    },
                    purpose="获取行情、估值和可用的市场快照指标",
                    approval_required=risk_level == "high" or market.risk_level == "high",
                )
            )
        financial = _tool_by_suffix(tool_specs, ".stock.financial_metrics")
        if financial is not None and "financial" in dimensions:
            requests.append(
                _tool_request(
                    tool=financial,
                    arguments={
                        "symbol": RESOLVED_SYMBOL_PLACEHOLDER,
                        "period": _period_from_message(user_message),
                        "required_fields": [],
                        "optional_fields": [
                            "revenue",
                            "net_profit_parent",
                            "operating_cash_flow",
                            "revenue_yoy",
                            "net_profit_yoy",
                            "operating_cash_flow_yoy",
                            "gross_margin",
                            "roe",
                            "debt_to_asset",
                        ],
                    },
                    purpose="获取财务质量与成长性相关指标，缺失时降级披露",
                    approval_required=risk_level == "high" or financial.risk_level == "high",
                )
            )
        announcements = _tool_by_suffix(tool_specs, ".stock.announcement_search")
        if announcements is not None and ("risk" in dimensions or "business" in dimensions):
            requests.append(
                _tool_request(
                    tool=announcements,
                    arguments={
                        "symbol": RESOLVED_SYMBOL_PLACEHOLDER,
                        "date_range": "12m",
                        "required_fields": [],
                        "optional_fields": ["announcements"],
                    },
                    purpose="检索近期公告和事件线索，作为风险与业务变化证据",
                    approval_required=(
                        risk_level == "high" or announcements.risk_level == "high"
                    ),
                )
            )
    rag = tool_specs.get("rag_search")
    if rag is not None and task["need_rag"]:
        requests.append(
            _tool_request(
                tool=rag,
                arguments={
                    "query": user_message,
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "top_k": 8,
                    "retrieval_mode": "hybrid",
                },
                purpose="检索知识库中的公告、研报或长期 knowledge 证据",
                approval_required=risk_level == "high" or rag.risk_level == "high",
            )
        )
    return requests


def _tool_by_suffix(tool_specs: dict[str, ToolSpec], suffix: str) -> ToolSpec | None:
    return next(
        (tool for name, tool in tool_specs.items() if name.endswith(suffix)),
        None,
    )


def _tool_request(
    *,
    tool: ToolSpec,
    arguments: dict,
    purpose: str,
    approval_required: bool,
) -> ToolCallRequest:
    return ToolCallRequest(
        tool_name=tool.name,
        tool_version=tool.version,
        arguments=arguments,
        purpose=purpose,
        approval_required=approval_required,
    )


def _period_from_message(user_message: str) -> str:
    year_match = re.search(r"(20\d{2})", user_message)
    if year_match is None:
        return "latest"
    year = year_match.group(1)
    if re.search(r"Q1|一季报", user_message, re.IGNORECASE):
        return f"{year}Q1"
    if re.search(r"半年报", user_message, re.IGNORECASE):
        return f"{year}H1"
    if re.search(r"Q2|二季报", user_message, re.IGNORECASE):
        return f"{year}Q2"
    if re.search(r"Q3|三季报", user_message, re.IGNORECASE):
        return f"{year}Q3"
    if re.search(r"Q4|四季报", user_message, re.IGNORECASE):
        return f"{year}Q4"
    return year
