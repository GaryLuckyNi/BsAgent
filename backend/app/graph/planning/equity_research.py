import re

from app.graph.planning.templates import build_risk_controls
from app.graph.state import AgentState
from app.llm.factory import LlmClient
from app.schemas.tools import ToolCallRequest, ToolSpec
from app.skills.configured import RESOLVED_SYMBOL_PLACEHOLDER, extract_company_query


_CAPABILITY_MARKERS = {
    "business": ("主营", "业务", "商业模式", "产品", "客户", "竞争力"),
    "market": ("行情", "股价", "涨跌", "成交", "换手", "市值", "市场状态"),
    "valuation": ("估值", "PE", "PB", "市盈率", "市净率"),
    "financial": (
        "财务",
        "财报",
        "年报",
        "利润",
        "盈利",
        "现金流",
        "毛利率",
        "ROE",
        "负债",
    ),
    "risk": ("风险", "问询", "处罚", "诉讼", "减持", "质押", "下行"),
    "technical": ("技术", "K线", "k线", "走势", "均线", "波动", "回撤"),
}


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
    has_web = any(request.tool_name == "web_search" for request in requested_tools)
    return {
        "objective": task["user_goal"],
        "execution_mode": "tool_execution",
        "selected_skill": {
            "name": intent.get("candidate_skill") or "equity_research",
            "selection_reason": "equity_research_dynamic_evidence_plan",
        },
        "analysis_questions": task["analysis_questions"],
        "evidence_channels": {
            "mcp": True,
            "web_search": task["need_web_search"],
            "rag": task["need_rag"],
        },
        "subtasks": _build_analysis_subtasks(task),
        "analysis_dimensions": task["analysis_dimensions"],
        "answer_depth": task["answer_depth"],
        "execution_steps": [
            "understand_equity_research_intent",
            "build_market_data_evidence_plan",
            "execute_mcp_tools",
            *(["search_public_web"] if has_web else []),
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
            "decision": "planner_emitted_task_brief_evidence_requests",
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
    del intent, llm_client
    task_brief = state.get("task_brief") or {}
    questions = _bounded_strings(task_brief.get("analysis_questions"), limit=12)
    user_goal = _safe_text(task_brief.get("user_goal"), fallback=state["user_message"])
    if not questions:
        questions = [user_goal]
    company_query = _company_query_from_task_brief(task_brief, state["user_message"])
    analysis_dimensions = _analysis_dimensions(
        user_goal=user_goal,
        analysis_questions=questions,
    )
    return {
        "user_goal": user_goal,
        "company_query": company_query,
        "analysis_questions": questions,
        "analysis_dimensions": analysis_dimensions,
        "need_mcp": True,
        "need_web_search": task_brief.get("web_search_required") is True,
        "need_rag": task_brief.get("rag_required") is True,
        "search_keywords": _bounded_strings(
            task_brief.get("search_keywords"), limit=12
        ),
        "answer_depth": "standard",
    }


def _company_query_from_task_brief(task_brief: dict, user_message: str) -> str:
    entities = task_brief.get("entities")
    if isinstance(entities, list):
        for entity in entities:
            if not isinstance(entity, dict):
                continue
            if entity.get("type") not in {"company", "security"}:
                continue
            mention = entity.get("mention")
            if isinstance(mention, str) and mention.strip():
                return mention.strip()
    return extract_company_query(user_message)


def _safe_text(value: object, *, fallback: str) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else fallback


def _bounded_strings(value: object, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized = [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return list(dict.fromkeys(normalized))[:limit]


def _analysis_dimensions(
    *, user_goal: str, analysis_questions: list[str]
) -> list[str]:
    task_text = " ".join([user_goal, *analysis_questions])
    return [
        capability
        for capability, markers in _CAPABILITY_MARKERS.items()
        if any(marker in task_text for marker in markers)
    ]


def _build_analysis_subtasks(task: dict) -> list[dict]:
    subtasks = [
        {
            "id": "resolve_company",
            "objective": f"确认 {task['company_query']} 对应的 A 股主体",
            "depends_on": [],
            "capability_query": "A 股公司名称、简称或代码的实体标准化",
            "evidence_requirements": ["证券代码", "证券名称"],
            "success_criteria": "获得可供后续结构化查询使用的唯一证券标识",
        }
    ]
    for index, question in enumerate(task["analysis_questions"], start=1):
        subtasks.append(
            {
                "id": f"analysis_{index}",
                "objective": question,
                "depends_on": ["resolve_company"],
                "capability_query": question,
                "evidence_requirements": _evidence_requirements(question),
                "success_criteria": "获得可引用、可核验且标明时间边界的事实",
            }
        )
    return subtasks


def _evidence_requirements(question: str) -> list[str]:
    requirements = []
    for capability, markers in _CAPABILITY_MARKERS.items():
        if any(marker in question for marker in markers):
            requirements.append(
                {
                    "business": "公司业务事实",
                    "market": "结构化行情数据",
                    "valuation": "结构化估值指标",
                    "financial": "结构化财务指标",
                    "risk": "近期公开风险证据",
                    "technical": "结构化价格趋势数据",
                }[capability]
            )
    return requirements or ["与分析问题直接相关的可核验证据"]


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
        if profile is not None and "business" in dimensions:
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
    web_search = tool_specs.get("web_search")
    if web_search is not None and task["need_web_search"]:
        requests.append(
            _tool_request(
                tool=web_search,
                arguments={
                    "query": _web_search_query(
                        task["search_keywords"], fallback=user_message
                    ),
                    "max_results": 5,
                },
                purpose="检索任务明确需要的近期公开网页证据",
                approval_required=(
                    risk_level == "high" or web_search.risk_level == "high"
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


def _web_search_query(search_keywords: list[str], *, fallback: str) -> str:
    query = " ".join(search_keywords) or fallback.strip()
    return query[:500]


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
