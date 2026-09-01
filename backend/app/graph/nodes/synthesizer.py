import asyncio
import json
import re
from collections.abc import Callable

from langgraph.runtime import Runtime

from app.graph.state import AgentState, AnswerClaim
from app.llm.factory import LlmClient
from app.memory.preferences import preference_constraints, recall_user_preferences
from app.schemas.evidence import EvidencePackage


FORBIDDEN_BRAND_PATTERNS = (
    r"我是\s*DeepSeek[^\n。]*[。]?",
    r"作为\s*DeepSeek[^\n。]*[。]?",
    r"作为一个?大语言模型[^\n。]*[。]?",
    r"我是一个?AI助手[^\n。]*[。]?",
    r"DeepSeek",
)

FINANCIAL_DISCLAIMER = "以上内容仅用于投研辅助分析，不构成投资建议、买卖建议或收益承诺。"
PARTIAL_IMAGE_DISCLOSURE = "图片分析结果仅部分完成，以下结论需结合原图或其他资料复核。"
IMAGE_CONFLICT_DISCLOSURE = "OCR 与视觉模型对部分指标的识别存在冲突，相关数字不得作为确定事实。"
PARTIAL_MARKET_DATA_DISCLOSURE = "市场数据仅部分返回，部分字段未返回，相关结论需结合补充数据复核。"
SAFE_EQUITY_RESEARCH_FALLBACK = (
    "本次已经取得部分公司、行情或财务事实，但无法在现有证据约束内生成可靠的综合分析。"
    "请稍后重试，或缩小到财务、估值、行情等具体分析范围。"
)
EQUITY_RESEARCH_REQUIRED_SECTIONS = (
    "核心判断",
    "业务与行业",
    "财务质量",
    "行情与估值",
    "主要风险",
    "数据边界",
)
EQUITY_RESEARCH_SECTION_ALIASES = {
    "核心判断": ("核心结论", "综合判断"),
    "业务与行业": ("业务及行业", "业务分析", "业务与行业分析"),
    "财务质量": ("财务分析", "财务表现", "盈利与现金流"),
    "行情与估值": ("行情及估值", "市场表现与估值", "估值与行情"),
    "主要风险": ("风险提示", "核心风险", "风险因素"),
    "数据边界": ("数据局限", "分析边界", "数据与分析边界"),
}
EQUITY_RESEARCH_MAX_TOKENS = 2048
DATA_INSUFFICIENCY_MARKERS = (
    "数据不足",
    "未获得",
    "未返回",
    "无法判断",
    "需补充",
)
CLAIM_REFERENCE_PATTERN = re.compile(r"\[((?:tool|evidence)_claim_\d+)\]")
CLAIM_LIKE_REFERENCE_PATTERN = re.compile(r"\[([A-Za-z0-9_]*claim_\d+)\]")
COMPANY_EVENT_TERMS = (
    "公告",
    "处罚",
    "诉讼",
    "立案",
    "减持",
    "质押",
    "重组",
    "并购",
    "收购",
    "重大资产购买",
    "控制权变更",
    "停复牌",
    "停牌",
    "复牌",
    "监管问询",
    "问询函",
    "财务造假",
)
PROHIBITED_FINANCIAL_ADVICE_PATTERNS = (
    re.compile(r"目标(?:价|价格|股价)"),
    re.compile(
        r"(?:预计|预期|预测|判断|确定|必然|肯定).{0,20}"
        r"(?:上涨|下跌|涨至|跌至|涨到|跌到|涨幅|跌幅)"
    ),
    re.compile(r"(?:承诺|保证|确保|稳赚|必赚|保本).{0,20}(?:收益|回报|盈利|赚钱)"),
    re.compile(
        r"(?:建议|推荐|应当|应该|适合|值得|必须|可以).{0,12}"
        r"(?:直接)?(?:买入|卖出|加仓|减仓|持有|抄底)"
    ),
    re.compile(r"(?:股价.{0,8})?(?:未来|后续).{0,12}(?:上涨|下跌)"),
    re.compile(r"(?<!不)(?<!勿)(?<!避免)(?:看涨|看跌)"),
    re.compile(r"(?:合理估值中枢|上行空间|下行空间|逢低布局|低吸|高抛|配置机会)"),
    re.compile(r"(?:建议|推荐|布局|买入|卖出)"),
)
MODEL_BRAND_TOKEN_PATTERN = re.compile(
    r"(?:OpenAI|ChatGPT|Claude|Anthropic|Gemini|DeepSeek|Qwen|"
    r"通义千问|豆包|Kimi|Moonshot|文心一言|智谱|讯飞星火|MiniMax|Llama|Mistral)",
    flags=re.IGNORECASE,
)

SECTION_ALLOWED_FIELDS = {
    "业务与行业": {
        "name",
        "industry",
        "main_business",
        "board",
        "registered_address",
    },
    "财务质量": {
        "revenue",
        "net_profit_parent",
        "net_profit",
        "operating_cash_flow",
        "revenue_yoy",
        "net_profit_yoy",
        "operating_cash_flow_yoy",
        "gross_margin",
        "roe",
        "debt_to_asset",
    },
    "行情与估值": {
        "price",
        "pct_change",
        "pct_chg",
        "volume",
        "turnover",
        "amount",
        "market_cap",
        "total_mv",
        "circ_mv",
        "pe_ttm",
        "pb",
        "latest_price",
        "return_20d",
        "return_60d",
        "ma20",
        "ma60",
        "volatility_20d",
        "max_drawdown",
    },
}
FIELD_LABEL_ALIASES = {
    "name": ("公司名称",),
    "industry": ("所属行业", "行业"),
    "main_business": ("主营业务",),
    "board": ("上市板块",),
    "registered_address": ("注册地址",),
    "revenue": ("营业收入",),
    "net_profit_parent": ("归母净利润",),
    "net_profit": ("净利润",),
    "operating_cash_flow": ("经营活动现金流", "经营现金流", "现金流"),
    "revenue_yoy": ("营业收入同比", "营收同比"),
    "net_profit_yoy": ("归母净利润同比",),
    "operating_cash_flow_yoy": ("经营活动现金流同比", "经营现金流同比"),
    "gross_margin": ("毛利率",),
    "roe": ("净资产收益率", "ROE"),
    "debt_to_asset": ("资产负债率", "debt_to_asset"),
    "price": ("最新价格", "股价"),
    "pct_change": ("涨跌幅",),
    "pct_chg": ("涨跌幅",),
    "volume": ("成交量",),
    "turnover": ("成交额",),
    "amount": ("成交额",),
    "market_cap": ("总市值",),
    "total_mv": ("总市值",),
    "circ_mv": ("流通市值",),
    "pe_ttm": ("市盈率", "PE"),
    "pb": ("市净率", "PB"),
    "latest_price": ("最新收盘价",),
    "return_20d": ("近20日收益率",),
    "return_60d": ("近60日收益率",),
    "ma20": ("20日均价", "MA20"),
    "ma60": ("60日均价", "MA60"),
    "volatility_20d": ("近20日年化波动率",),
    "max_drawdown": ("区间最大回撤",),
}
MONEY_FIELDS = {
    "turnover",
    "amount",
    "market_cap",
    "total_mv",
    "circ_mv",
    "revenue",
    "net_profit_parent",
    "net_profit",
    "operating_cash_flow",
}
RATIO_FIELDS = {
    "gross_margin",
    "roe",
    "debt_to_asset",
    "return_20d",
    "return_60d",
    "volatility_20d",
    "max_drawdown",
    "revenue_yoy",
    "net_profit_yoy",
    "operating_cash_flow_yoy",
}
PERCENT_POINT_FIELDS = {"pct_change", "pct_chg"}
NUMBER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(?:\d{4}-\d{1,2}-\d{1,2}|[+-]?\d+(?:\.\d+)?%?)"
    r"(?![A-Za-z0-9_]|\.[A-Za-z])"
)

SAFE_MARKET_ERROR_MESSAGES = {
    "market_data_source_not_configured": "当前未配置市场数据源，请先在管理中心完成配置。",
    "market_data_configuration_invalid": "市场数据源配置无效，请联系管理员检查配置。",
    "market_data_configuration_unavailable": "市场数据源配置暂时不可用，请稍后重试。",
    "invalid_field_request": "本次请求包含当前市场数据能力不支持的字段。",
    "required_api_permission_missing": "当前数据源权限不足，无法获取本次分析所需数据。",
    "provider_rate_limited": "市场数据服务当前访问频繁，请稍后重试。",
    "provider_authentication_failed": "市场数据源认证失败，请联系管理员检查配置。",
    "provider_permission_denied": "当前数据源权限不足，无法获取本次分析所需数据。",
    "provider_invalid_request": "市场数据请求无效，无法完成本次分析。",
    "provider_timeout": "市场数据服务响应超时，请稍后重试。",
    "provider_unavailable": "市场数据服务暂时不可用，请稍后重试。",
    "provider_upstream_error": "市场数据服务暂时不可用，请稍后重试。",
    "provider_response_invalid": "市场数据服务返回异常，暂时无法生成可靠分析。",
    "required_data_missing": "市场数据源未返回本次分析所需的必需字段。",
    "unsupported_capability": "当前市场数据源不支持本次分析所需能力。",
}

TOOL_FIELD_LABELS = {
    "symbol": "证券代码",
    "name": "公司名称",
    "exchange": "交易所",
    "industry": "所属行业",
    "board": "上市板块",
    "main_business": "主营业务",
    "registered_address": "注册地址",
    "price": "最新价格",
    "pct_change": "涨跌幅",
    "pct_chg": "涨跌幅",
    "volume": "成交量",
    "turnover": "成交额",
    "amount": "成交额",
    "market_cap": "总市值",
    "pe_ttm": "市盈率（TTM）",
    "pb": "市净率",
    "total_mv": "总市值",
    "circ_mv": "流通市值",
    "revenue": "营业收入",
    "net_profit": "净利润",
    "net_profit_parent": "归母净利润",
    "gross_margin": "毛利率",
    "roe": "净资产收益率",
    "operating_cash_flow": "经营活动现金流",
    "revenue_yoy": "营业收入同比",
    "net_profit_yoy": "归母净利润同比",
    "operating_cash_flow_yoy": "经营活动现金流同比",
    "debt_to_asset": "资产负债率",
    "announcements": "公司公告",
    "latest_price": "最新收盘价",
    "return_20d": "近20日收益率",
    "return_60d": "近60日收益率",
    "ma20": "20日均价",
    "ma60": "60日均价",
    "volatility_20d": "近20日年化波动率",
    "max_drawdown": "区间最大回撤",
}


def _safe_handled_error_message(error: dict) -> str:
    code = error.get("code")
    if isinstance(code, str) and code in SAFE_MARKET_ERROR_MESSAGES:
        message = SAFE_MARKET_ERROR_MESSAGES[code]
        if code in {"required_api_permission_missing", "required_data_missing"}:
            details = error.get("details")
            raw_missing_fields = (
                details.get("missing_fields") if isinstance(details, dict) else None
            )
            missing_fields = (
                raw_missing_fields if isinstance(raw_missing_fields, list) else []
            )
            safe_fields = [
                field
                for field in missing_fields
                if isinstance(field, str)
                and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", field)
            ][:16]
            if safe_fields:
                message = f"{message}缺失字段：{'、'.join(safe_fields)}。"
        return message
    message = error.get("message")
    return message if isinstance(message, str) else "请求执行失败，请稍后重试。"


def build_synthesizer_node(
    *,
    llm_client: LlmClient | None = None,
    on_memory_recall_failure: Callable[[AgentState], None] | None = None,
):
    def node(state: AgentState, runtime: Runtime | None = None) -> AgentState:
        evidence = state["evidence"]
        analysis = state["analysis"] or {}
        plan = state["plan"] or {}
        plan_response_policy = plan.get("response_policy", {})
        missing_information = analysis.get("missing_information", [])
        response_policy = _build_response_policy(
            evidence=evidence,
            llm_client=llm_client,
        )
        response_policy.update(plan_response_policy)
        response_policy.update(
            preference_constraints(
                recall_user_preferences(runtime, state, on_failure=on_memory_recall_failure)
            )
        )

        handled_error = next(
            (error for error in reversed(state["errors"]) if error.get("handled")),
            None,
        )
        if handled_error is not None:
            response_policy["response_type"] = "execution_error_response"
            error_code = handled_error.get("code", "tool_execution_failed")
            state["draft_answer"] = {
                "content": _finalize_response_content(_safe_handled_error_message(handled_error)),
                "claims": [],
                "cited_evidence_ids": [],
                "missing_information": [error_code],
                "assumptions": [],
                "confidence": 0.0,
                "response_policy": response_policy,
                "response_type": "execution_error_response",
                "business_error_code": error_code,
            }
            return state

        if plan.get("execution_mode") == "direct_response":
            content = _render_direct_response_content(
                state=state,
                response_policy=response_policy,
                llm_client=llm_client,
            )
            state["draft_answer"] = {
                "content": _finalize_response_content(content),
                "claims": [],
                "cited_evidence_ids": [],
                "missing_information": missing_information,
                "assumptions": [],
                "confidence": (state["intent"] or {}).get("confidence", 0.8),
                "response_policy": response_policy,
            }
            return state
        if (
            plan.get("execution_mode") == "tool_execution"
            and not response_policy.get("response_type", "").startswith("equity_research_")
        ):
            content = _render_tool_execution_content(
                state=state,
                response_policy=response_policy,
                llm_client=llm_client,
            )
            state["draft_answer"] = {
                "content": _finalize_response_content(content),
                "claims": [],
                "cited_evidence_ids": [],
                "missing_information": [] if state["tool_calls"] else ["没有工具调用结果"],
                "assumptions": [],
                "confidence": 1.0 if state["tool_calls"] else 0.0,
                "response_policy": response_policy,
            }
            return state

        if plan.get("execution_mode") == "image_analysis":
            content = _render_image_analysis_content(
                state=state,
                response_policy=response_policy,
                llm_client=llm_client,
            )
            state["draft_answer"] = {
                "content": _finalize_response_content(content),
                "claims": [],
                "cited_evidence_ids": [],
                "missing_information": missing_information,
                "assumptions": [],
                "confidence": analysis.get("confidence", 0.0),
                "response_policy": response_policy,
            }
            return state

        if response_policy.get("response_type", "").startswith("equity_research_"):
            claims = _build_equity_claims(state)
            cited_evidence_ids = list(
                dict.fromkeys(
                    evidence_id
                    for claim in claims
                    for evidence_id in claim["evidence_ids"]
                )
            )
            content = _render_equity_research_content(
                state=state,
                response_policy=response_policy,
                claims=claims,
                llm_client=llm_client,
            )
            state["draft_answer"] = {
                "content": _finalize_response_content(content),
                "claims": claims,
                "cited_evidence_ids": cited_evidence_ids,
                "missing_information": missing_information,
                "assumptions": [],
                "confidence": analysis.get("confidence", 0.6),
                "response_policy": response_policy,
            }
            return state

        if evidence is None or not evidence.items:
            content = "当前知识库没有检索到足够证据，无法给出可靠结论。"
            state["draft_answer"] = {
                "content": _finalize_response_content(content),
                "claims": [],
                "cited_evidence_ids": [],
                "missing_information": missing_information or ["没有可用证据"],
                "assumptions": [],
                "confidence": 0.0,
                "response_policy": response_policy,
            }
            return state

        claims = _build_fact_claims(evidence)
        cited_evidence_ids = [
            evidence_id
            for claim in claims
            for evidence_id in claim["evidence_ids"]
        ]
        confidence = _draft_confidence(
            analysis_confidence=analysis.get("confidence"),
            claims=claims,
            coverage=evidence.coverage,
        )
        content = _render_content(
            claims=claims,
            missing_information=missing_information,
            coverage=evidence.coverage,
        )
        rendered_content = _render_with_llm(
            content=content,
            claims=claims,
            missing_information=missing_information,
            response_policy=response_policy,
            llm_client=llm_client,
        )
        state["draft_answer"] = {
            "content": _finalize_response_content(rendered_content),
            "claims": claims,
            "cited_evidence_ids": cited_evidence_ids,
            "missing_information": missing_information,
            "assumptions": [],
            "confidence": confidence,
            "response_policy": response_policy,
        }
        return state

    return node


def synthesizer_node(state: AgentState, runtime: Runtime | None = None) -> AgentState:
    return build_synthesizer_node()(state, runtime)


def _render_image_analysis_content(
    *,
    state: AgentState,
    response_policy: dict,
    llm_client: LlmClient | None,
) -> str:
    attachments = state["image_analyses"]
    fallback_sections = []
    for index, attachment in enumerate(attachments, start=1):
        metrics = attachment["financial_metrics"]
        metrics_text = json.dumps(metrics, ensure_ascii=False) if metrics else "未识别到"
        risks = "；".join(attachment["risk_flags"]) or "未识别到明确风险提示"
        fallback_sections.append(
            f"图片 {index}\n"
            f"{attachment['summary']}\n"
            f"关键财务指标：{metrics_text}\n"
            f"识别限制：{risks}"
        )
    body = "\n\n".join(fallback_sections)
    if llm_client is not None:
        try:
            result = asyncio.run(
                llm_client.complete(
                    system_prompt=(
                        "你是 BsAgent 的 A 股投研图片回答器，不代表任何模型厂商。"
                        "只能依据 validated_image_context 回答用户问题，不得新增图片中没有的事实。"
                        "金额、单位、期间和同比/环比不得改写失真；明确说明识别限制。"
                        "不得提供买卖建议、收益承诺或无依据目标价。使用专业中文。"
                    ),
                    user_prompt=json.dumps(
                        {
                            "user_message": state["user_message"],
                            "validated_image_context": attachments,
                            "response_policy": response_policy,
                        },
                        ensure_ascii=False,
                    ),
                    temperature=0.1,
                    max_tokens=768,
                    thinking=False,
                )
            )
            body = result.content
            response_policy["llm_provider"] = result.provider
            response_policy["llm_model"] = result.model
            response_policy["llm_usage"] = result.usage
        except Exception:
            response_policy["llm_rendering"] = "failed"
    if (
        any(attachment["status"] == "partial" for attachment in attachments)
        and PARTIAL_IMAGE_DISCLOSURE not in body
    ):
        body = f"{PARTIAL_IMAGE_DISCLOSURE}\n\n{body}"
    body = _ensure_image_conflict_disclosure(body=body, state=state)
    if FINANCIAL_DISCLAIMER not in body:
        body = f"{body}\n\n免责声明\n{FINANCIAL_DISCLAIMER}"
    return body


def _render_equity_research_content(
    *,
    state: AgentState,
    response_policy: dict,
    claims: list[AnswerClaim],
    llm_client: LlmClient | None,
) -> str:
    deterministic = _equity_claims_template(
        claims=claims,
        response_type=response_policy.get("response_type", ""),
    )
    body = deterministic
    if llm_client is not None:
        body = _render_equity_research_with_llm(
            state=state,
            deterministic=deterministic,
            claims=claims,
            response_policy=response_policy,
            llm_client=llm_client,
        )

    body = _ensure_partial_market_data_disclosure(body=body, state=state)
    body = _ensure_partial_image_disclosure(body=body, state=state)
    body = _ensure_image_conflict_disclosure(body=body, state=state)
    if FINANCIAL_DISCLAIMER not in body:
        body = f"{body}\n\n免责声明\n{FINANCIAL_DISCLAIMER}"
    return body


def _render_equity_research_with_llm(
    *,
    state: AgentState,
    deterministic: str,
    claims: list[AnswerClaim],
    response_policy: dict,
    llm_client: LlmClient,
) -> str:
    user_prompt = _equity_research_user_prompt(
        state=state,
        deterministic=deterministic,
        claims=claims,
        response_policy=response_policy,
    )
    try:
        first_result = _complete_equity_research(
            llm_client=llm_client,
            system_prompt=_equity_research_system_prompt(),
            user_prompt=user_prompt,
        )
    except Exception:
        response_policy["llm_rendering"] = "safe_fallback"
        response_policy["llm_validation_error"] = "primary_call_failed"
        return SAFE_EQUITY_RESEARCH_FALLBACK

    first_content = _remove_unsupported_temporal_context(
        rendered=first_result.content,
        claims=claims,
    )
    first_error = _equity_rendering_error(
        rendered=first_content,
        claims=claims,
        response_type=response_policy.get("response_type", ""),
    )
    if first_error is None:
        first_rendered = _prepare_equity_content_for_user(first_content)
        _record_llm_rendering(
            response_policy=response_policy,
            result=first_result,
            rendering="applied",
        )
        return first_rendered

    first_salvaged = (
        _salvage_equity_report(
            rendered=first_content,
            claims=claims,
        )
        if first_error == "unsupported_numbers"
        else None
    )
    if first_salvaged is not None:
        response_policy["llm_validation_error"] = first_error
        _record_llm_rendering(
            response_policy=response_policy,
            result=first_result,
            rendering="salvaged",
        )
        return _prepare_equity_content_for_user(first_salvaged)

    try:
        repair_result = _complete_equity_research(
            llm_client=llm_client,
            system_prompt=_equity_research_system_prompt(repair_reason=first_error),
            user_prompt=user_prompt,
        )
    except Exception:
        response_policy["llm_rendering"] = "deterministic_fallback"
        response_policy["llm_validation_error"] = first_error
        return _prepare_equity_content_for_user(deterministic)

    repair_content = _remove_unsupported_temporal_context(
        rendered=repair_result.content,
        claims=claims,
    )
    repair_error = _equity_rendering_error(
        rendered=repair_content,
        claims=claims,
        response_type=response_policy.get("response_type", ""),
    )
    if repair_error is not None:
        salvaged = _salvage_equity_report(
            rendered=repair_content,
            claims=claims,
        )
        if salvaged is not None:
            response_policy["llm_validation_error"] = repair_error
            _record_llm_rendering(
                response_policy=response_policy,
                result=repair_result,
                rendering="salvaged",
            )
            return _prepare_equity_content_for_user(salvaged)
        response_policy["llm_rendering"] = "deterministic_fallback"
        response_policy["llm_validation_error"] = repair_error
        if repair_error == "unsupported_numbers":
            response_policy["llm_validation_details"] = {
                "unsupported_number_tokens": _unsupported_number_tokens(
                    rendered=repair_content,
                    claims=claims,
                )[:20]
            }
        return _prepare_equity_content_for_user(deterministic)

    repaired = _prepare_equity_content_for_user(repair_content)
    _record_llm_rendering(
        response_policy=response_policy,
        result=repair_result,
        rendering="repaired",
    )
    response_policy.pop("llm_validation_error", None)
    return repaired


def _complete_equity_research(*, llm_client: LlmClient, system_prompt: str, user_prompt: str):
    return asyncio.run(
        llm_client.complete(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=0.0,
            max_tokens=EQUITY_RESEARCH_MAX_TOKENS,
            thinking=False,
        )
    )


def _equity_research_system_prompt(*, repair_reason: str | None = None) -> str:
    repair_instruction = ""
    if repair_reason is not None:
        repair_instruction = (
            f"上一次输出因 {repair_reason} 未通过校验。请只修复该问题，"
            "继续使用同一组证据，不得补充任何新事实。"
        )
    return (
        "你是 BsAgent 中证据约束下的 A 股投研分析师。"
        "请结合 canonical claims、用户问题、analysis_dimensions、"
        "structured_tool_results 和 data_coverage 形成定性分析，不要逐字段复述。"
        "宽泛公司分析必须组织为：核心判断、业务与行业、财务质量、行情与估值、"
        "主要风险、数据边界。缺少证据的维度必须明确说明，不得猜测。"
        "请将这六个标题原样单独成行，不要改名。"
        "当 response_type 为 equity_research_report 时，每个有分析内容的段落或章节"
        "必须附上一个或多个现有 claim_id，可统一放在段落末尾，"
        "严格使用 [tool_claim_1] 或 "
        "[evidence_claim_2] 格式；不得编造 claim_id。示例："
        "归母净利润为负，盈利承压。[tool_claim_2]"
        "数据不足的章节可以不引用，但正文必须明确写数据不足、未获得、未返回、"
        "无法判断或需补充。六个章节都必须有非空正文。"
        "允许依据已提供事实作出盈利承压、经营现金流承压、估值指标较高等定性判断。"
        "在数据边界章节末尾，根据已缺失或尚未交叉验证的维度给出一至两个具体的下一步研究方向，"
        "例如连续报告期趋势、行业可比公司或最新公告；不得把下一步研究写成投资建议。"
        "不得新增 claims 中没有的数字、公司事件、目标价、涨跌预测或收益承诺，"
        "不得提及模型厂商品牌。使用专业、审慎、自然的中文。"
        f"{repair_instruction}"
    )


def _equity_research_user_prompt(
    *,
    state: AgentState,
    deterministic: str,
    claims: list[AnswerClaim],
    response_policy: dict,
) -> str:
    analysis = state["analysis"] or {}
    plan = state["plan"] or {}
    return json.dumps(
        {
            "user_message": state["user_message"],
            "analysis_dimensions": plan.get("analysis_dimensions", []),
            "claims": claims,
            "structured_tool_results": analysis.get("structured_tool_results", []),
            "data_coverage": analysis.get("data_coverage", []),
            "missing_information": analysis.get("missing_information", []),
            "deterministic_fallback": deterministic,
            "response_policy": response_policy,
        },
        ensure_ascii=False,
    )


def _equity_rendering_error(
    *,
    rendered: str,
    claims: list[AnswerClaim],
    response_type: str,
) -> str | None:
    if _contains_unsupported_numbers(rendered=rendered, claims=claims):
        return "unsupported_numbers"
    if _contains_prohibited_financial_advice(rendered):
        return "prohibited_financial_advice"
    if MODEL_BRAND_TOKEN_PATTERN.search(rendered):
        return "prohibited_model_brand"
    if response_type == "equity_research_report":
        sections = _parse_equity_research_sections(rendered)
        if sections is None or any(not sections[section] for section in sections):
            return "missing_required_sections"
        contract_error = _equity_report_contract_error(
            sections=sections,
            claims=claims,
        )
        if contract_error is not None:
            return contract_error
    elif _contains_unsupported_company_event(rendered=rendered, claims=claims):
        return "unsupported_company_event"
    if not _contains_required_claim_signals(rendered=rendered, claims=claims):
        return "claim_signals_missing"
    return None


def _contains_prohibited_financial_advice(rendered: str) -> bool:
    return any(pattern.search(rendered) for pattern in PROHIBITED_FINANCIAL_ADVICE_PATTERNS)


def _equity_report_contract_error(
    *,
    sections: dict[str, str],
    claims: list[AnswerClaim],
) -> str | None:
    claims_by_id = {claim["claim_id"]: claim for claim in claims}
    for section_name, body in sections.items():
        section_claim_ids = set(CLAIM_REFERENCE_PATTERN.findall(body))
        section_claim_like_ids = set(CLAIM_LIKE_REFERENCE_PATTERN.findall(body))
        if section_claim_like_ids - claims_by_id.keys():
            return "invalid_claim_reference"
        section_claims = [claims_by_id[item] for item in section_claim_ids]
        section_has_analysis = False
        for statement in _section_statements(body):
            referenced_ids = set(CLAIM_REFERENCE_PATTERN.findall(statement))
            statement_without_refs = CLAIM_REFERENCE_PATTERN.sub("", statement).strip()
            data_insufficient = _is_data_insufficient(statement_without_refs)
            referenced_claims = (
                [claims_by_id[item] for item in referenced_ids]
                if referenced_ids
                else section_claims
            )
            event_error = _statement_event_error(
                statement=statement_without_refs,
                referenced_claims=referenced_claims,
                data_insufficient=data_insufficient,
            )
            if event_error is not None:
                return event_error

            if not referenced_claims and not data_insufficient:
                return "invalid_claim_reference"
            if _contains_unsupported_numbers(
                rendered=statement_without_refs,
                claims=referenced_claims,
            ):
                return "mismatched_claim_number"

            if referenced_claims:
                section_has_analysis = True
                referenced_fields = {
                    field_name
                    for claim in referenced_claims
                    for field_name in _claim_field_names(claim)
                }
                if _statement_has_unsupported_field_label(
                    statement=statement_without_refs,
                    referenced_fields=referenced_fields,
                ):
                    return "unsupported_section_analysis"
            elif not data_insufficient:
                section_has_analysis = True

        if section_has_analysis:
            section_error = _section_claim_type_error(
                section_name=section_name,
                referenced_claims=section_claims,
            )
            if section_error is not None:
                return section_error
    return None


def _salvage_equity_report(
    *,
    rendered: str,
    claims: list[AnswerClaim],
) -> str | None:
    sections = _parse_equity_research_sections(rendered)
    if sections is None:
        return None

    claims_by_id = {claim["claim_id"]: claim for claim in claims}
    salvaged_sections: list[str] = []
    for section_name, body in sections.items():
        section_claim_ids = set(CLAIM_REFERENCE_PATTERN.findall(body))
        claim_like_ids = set(CLAIM_LIKE_REFERENCE_PATTERN.findall(body))
        if claim_like_ids - claims_by_id.keys():
            return None
        section_claims = [claims_by_id[item] for item in section_claim_ids]
        kept_statements: list[str] = []
        for statement in _section_statements(body):
            referenced_ids = set(CLAIM_REFERENCE_PATTERN.findall(statement))
            referenced_claims = (
                [claims_by_id[item] for item in referenced_ids]
                if referenced_ids
                else section_claims
            )
            plain_statement = CLAIM_REFERENCE_PATTERN.sub("", statement).strip()
            data_insufficient = _is_data_insufficient(plain_statement)
            if _contains_prohibited_financial_advice(plain_statement):
                continue
            if MODEL_BRAND_TOKEN_PATTERN.search(plain_statement):
                continue
            if not referenced_claims and not data_insufficient:
                continue
            if _statement_event_error(
                statement=plain_statement,
                referenced_claims=referenced_claims,
                data_insufficient=data_insufficient,
            ) is not None:
                continue
            if _contains_unsupported_numbers(
                rendered=plain_statement,
                claims=referenced_claims,
            ):
                continue
            referenced_fields = {
                field_name
                for claim in referenced_claims
                for field_name in _claim_field_names(claim)
            }
            if _statement_has_unsupported_field_label(
                statement=plain_statement,
                referenced_fields=referenced_fields,
            ):
                continue
            kept_statements.append(statement)

        if kept_statements and _section_claim_type_error(
            section_name=section_name,
            referenced_claims=section_claims,
        ) is not None:
            kept_statements = []
        if kept_statements:
            salvaged_body = "".join(kept_statements)
            if section_claim_ids:
                salvaged_body += "".join(
                    f"[{claim_id}]" for claim_id in sorted(section_claim_ids)
                )
        else:
            salvaged_body = "该维度的可验证据未获得，无法判断。"
        salvaged_sections.append(f"{section_name}\n{salvaged_body}")

    salvaged = "\n\n".join(salvaged_sections)
    if _equity_rendering_error(
        rendered=salvaged,
        claims=claims,
        response_type="equity_research_report",
    ) is not None:
        return None
    return salvaged


def _parse_equity_research_sections(rendered: str) -> dict[str, str] | None:
    section_lines = {section: [] for section in EQUITY_RESEARCH_REQUIRED_SECTIONS}
    current_section: str | None = None
    seen_sections: set[str] = set()
    for line in rendered.splitlines():
        heading = _equity_section_heading(line)
        if heading is not None:
            current_section, inline_content = heading
            seen_sections.add(current_section)
            if inline_content:
                section_lines[current_section].append(inline_content)
            continue
        if current_section is not None:
            section_lines[current_section].append(line)
    if seen_sections != set(EQUITY_RESEARCH_REQUIRED_SECTIONS):
        return None
    return {
        section: "\n".join(lines).strip()
        for section, lines in section_lines.items()
    }


def _equity_section_heading(line: str) -> tuple[str, str] | None:
    normalized = line.strip().lstrip("#").strip().replace("**", "")
    normalized = re.sub(
        r"^(?:\d+|[一二三四五六七八九十]+)[.、．]\s*",
        "",
        normalized,
    )
    for section in EQUITY_RESEARCH_REQUIRED_SECTIONS:
        headings = (section, *EQUITY_RESEARCH_SECTION_ALIASES[section])
        for heading in headings:
            if normalized == heading:
                return section, ""
            for separator in (":", "："):
                prefix = f"{heading}{separator}"
                if normalized.startswith(prefix):
                    return section, normalized[len(prefix):].strip()
    return None


def _section_statements(body: str) -> list[str]:
    statements: list[str] = []
    pattern = re.compile(
        r".*?(?:[。！？；](?:\[(?:tool|evidence)_claim_\d+\])*)|.+$"
    )
    for line in body.splitlines():
        statements.extend(
            statement.strip()
            for statement in pattern.findall(line.strip())
            if statement.strip()
        )
    return statements


def _is_data_insufficient(value: str) -> bool:
    return any(marker in value for marker in DATA_INSUFFICIENCY_MARKERS)


def _claim_field_names(claim: AnswerClaim) -> set[str]:
    fields: set[str] = set()
    for reference in claim.get("tool_evidence_refs", []):
        field_path = reference.get("field_path") if isinstance(reference, dict) else None
        if isinstance(field_path, str):
            fields.add(field_path.rsplit(".", maxsplit=1)[-1])
    claim_text = claim["text"]
    for field_name, aliases in FIELD_LABEL_ALIASES.items():
        if any(alias in claim_text for alias in aliases):
            fields.add(field_name)
    return fields


def _statement_has_unsupported_field_label(
    *,
    statement: str,
    referenced_fields: set[str],
) -> bool:
    for field_name, aliases in FIELD_LABEL_ALIASES.items():
        for alias in aliases:
            if alias == "净利润" and "归母净利润" in statement:
                continue
            if alias in statement and field_name not in referenced_fields:
                equivalent_fields = {
                    candidate
                    for candidate, candidate_aliases in FIELD_LABEL_ALIASES.items()
                    if alias in candidate_aliases
                }
                if not equivalent_fields & referenced_fields:
                    return True
    return False


def _section_claim_type_error(
    *,
    section_name: str,
    referenced_claims: list[AnswerClaim],
) -> str | None:
    referenced_fields = {
        field_name
        for claim in referenced_claims
        for field_name in _claim_field_names(claim)
    }
    allowed_fields = SECTION_ALLOWED_FIELDS.get(section_name)
    if allowed_fields is not None and not referenced_fields & allowed_fields:
        return "unsupported_section_analysis"
    if section_name == "核心判断" and not referenced_claims:
        return "invalid_claim_reference"
    if section_name == "主要风险":
        risk_fields = SECTION_ALLOWED_FIELDS["财务质量"] | SECTION_ALLOWED_FIELDS[
            "行情与估值"
        ]
        has_risk_claim = any(
            "风险" in claim["text"]
            or any(term in claim["text"] for term in COMPANY_EVENT_TERMS)
            for claim in referenced_claims
        )
        if not has_risk_claim and not referenced_fields & risk_fields:
            return "unsupported_section_analysis"
    return None


def _statement_event_error(
    *,
    statement: str,
    referenced_claims: list[AnswerClaim],
    data_insufficient: bool,
) -> str | None:
    for term in COMPANY_EVENT_TERMS:
        if term not in statement:
            continue
        if data_insufficient:
            continue
        matching_claims = [claim for claim in referenced_claims if term in claim["text"]]
        if not matching_claims:
            return "unsupported_company_event"
        if (
            all(_event_term_is_negated(claim["text"], term) for claim in matching_claims)
            and not _event_term_is_negated(statement, term)
        ):
            return "unsupported_company_event"
    return None


def _event_term_is_negated(value: str, term: str) -> bool:
    term_index = value.find(term)
    if term_index < 0:
        return False
    prefix = value[max(0, term_index - 12):term_index]
    return bool(re.search(r"(?:无|未|没有|不存在|未发现)[^，。；;]{0,10}$", prefix))


def _contains_unsupported_company_event(
    *,
    rendered: str,
    claims: list[AnswerClaim],
) -> bool:
    for term in COMPANY_EVENT_TERMS:
        if term not in rendered:
            continue
        if _is_data_insufficient(rendered):
            continue
        matching_claims = [claim for claim in claims if term in claim["text"]]
        if not matching_claims:
            return True
        if (
            all(_event_term_is_negated(claim["text"], term) for claim in matching_claims)
            and not _event_term_is_negated(rendered, term)
        ):
            return True
    return False


def _contains_required_equity_research_sections(rendered: str) -> bool:
    sections = _parse_equity_research_sections(rendered)
    return sections is not None and all(sections.values())


def _prepare_equity_content_for_user(content: str) -> str:
    without_references = CLAIM_REFERENCE_PATTERN.sub("", content)
    without_references = re.sub(r"[ \t]+([，。！？；：])", r"\1", without_references)
    return _normalize_brand_voice(without_references)


def _record_llm_rendering(*, response_policy: dict, result, rendering: str) -> None:
    response_policy["llm_rendering"] = rendering
    response_policy["llm_provider"] = result.provider
    response_policy["llm_model"] = result.model
    response_policy["llm_usage"] = result.usage


def _ensure_partial_image_disclosure(*, body: str, state: AgentState) -> str:
    if (
        any(analysis["status"] == "partial" for analysis in state["image_analyses"])
        and PARTIAL_IMAGE_DISCLOSURE not in body
    ):
        return f"{PARTIAL_IMAGE_DISCLOSURE}\n\n{body}"
    return body


def _ensure_image_conflict_disclosure(*, body: str, state: AgentState) -> str:
    has_conflict = any(
        flag.startswith("ocr_vision_metric_conflict:")
        for analysis in state["image_analyses"]
        for flag in analysis["risk_flags"]
    )
    if has_conflict and IMAGE_CONFLICT_DISCLOSURE not in body:
        return f"{IMAGE_CONFLICT_DISCLOSURE}\n\n{body}"
    return body


def _ensure_partial_market_data_disclosure(*, body: str, state: AgentState) -> str:
    coverage = (state["analysis"] or {}).get("data_coverage", [])
    if (
        any(item.get("status") != "complete" for item in coverage)
        and PARTIAL_MARKET_DATA_DISCLOSURE not in body
    ):
        return f"{PARTIAL_MARKET_DATA_DISCLOSURE}\n\n{body}"
    return body


def _build_equity_claims(state: AgentState) -> list[AnswerClaim]:
    analysis = state["analysis"] or {}
    claims: list[AnswerClaim] = []
    seen_tool_facts: set[tuple[str, str]] = set()
    for fact in analysis.get("tool_facts", []):
        dedup_key = (fact["field_name"], _render_fact_value(fact["value"]))
        if dedup_key in seen_tool_facts:
            continue
        seen_tool_facts.add(dedup_key)
        claims.append(
            {
                "claim_id": f"tool_claim_{len(claims) + 1}",
                "text": _tool_fact_claim_text(
                    field_name=fact["field_name"],
                    value=fact["value"],
                ),
                "evidence_ids": [],
                "confidence": 1.0,
                "claim_type": "fact",
                "tool_evidence_refs": [fact["ref"]],
            }
        )

    evidence = state["evidence"]
    if evidence is not None:
        for item in evidence.items:
            claims.append(
                {
                    "claim_id": f"evidence_claim_{len(claims) + 1}",
                    "text": item.content,
                    "evidence_ids": [item.evidence_id],
                    "confidence": item.score,
                    "claim_type": "fact",
                }
            )
    return claims


def _tool_fact_claim_text(*, field_name: str, value: object) -> str:
    label = TOOL_FIELD_LABELS.get(field_name, field_name)
    if field_name == "announcements" and isinstance(value, list):
        summaries = []
        for item in value[:5]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "未命名公告")
            date = str(item.get("date") or item.get("ann_date") or "日期未知")
            summaries.append(f"{title}（{date}）")
        if summaries:
            return f"{label}包括：{'；'.join(summaries)}"
        return f"{label}当前未检索到记录"
    return f"{label}为{_render_fact_value(value)}"


def _render_fact_value(value: object) -> str:
    if isinstance(value, dict):
        parts = [
            f"{TOOL_FIELD_LABELS.get(str(key), str(key))}={_render_fact_value(item)}"
            for key, item in list(value.items())[:8]
        ]
        return "、".join(parts) if parts else "空"
    if isinstance(value, list):
        return "、".join(_render_fact_value(item) for item in value[:8]) or "空"
    if value is None:
        return "未返回"
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)


def _equity_claims_template(
    *,
    claims: list[AnswerClaim],
    response_type: str,
) -> str:
    tool_claims = [claim["text"] for claim in claims if claim.get("tool_evidence_refs")]
    knowledge_claims = [claim["text"] for claim in claims if claim["evidence_ids"]]
    tool_text = _join_claim_sentences(tool_claims) or "当前市场数据源未返回可确认字段。"
    knowledge_text = _join_claim_sentences(knowledge_claims[:5]) or "当前知识检索未返回可引用资料。"

    if response_type == "equity_research_financial_report_analysis":
        return (
            "财务指标\n"
            f"{tool_text}\n\n"
            "财报与公告证据\n"
            f"{knowledge_text}\n\n"
            "分析边界\n"
            "仅陈述已取得的结构化指标和可引用资料，缺失指标不作推断。"
        )
    if response_type == "equity_research_risk_scan":
        return (
            "风险扫描\n"
            f"{tool_text}\n\n"
            "公告与知识证据\n"
            f"{knowledge_text}\n\n"
            "后续关注\n"
            "继续核对公告、监管问询、处罚、诉讼、减持和质押变化。"
        )
    if response_type == "equity_research_report":
        business_text = _section_claim_text(claims, SECTION_ALLOWED_FIELDS["业务与行业"])
        financial_text = _section_claim_text(claims, SECTION_ALLOWED_FIELDS["财务质量"])
        market_text = _section_claim_text(claims, SECTION_ALLOWED_FIELDS["行情与估值"])
        risk_text = _section_claim_text(claims, {"announcements"})
        available_dimensions = [
            label
            for label, text in (
                ("业务与行业", business_text),
                ("财务质量", financial_text),
                ("行情与估值", market_text),
                ("风险事件", risk_text or _join_claim_sentences(knowledge_claims[:5])),
            )
            if text
        ]
        core_text = (
            f"本次已取得{'、'.join(available_dimensions)}证据，以下按投研维度分别判断。"
            if available_dimensions
            else "当前未取得足以支持公司判断的结构化市场数据或知识证据。"
        )
        return (
            "核心判断\n"
            f"{core_text}\n\n"
            "业务与行业\n"
            f"{business_text or '该维度的可验证据未获得，无法判断。'}\n\n"
            "财务质量\n"
            f"{financial_text or '该维度的可验证据未获得，无法判断。'}\n\n"
            "行情与估值\n"
            f"{market_text or '该维度的可验证据未获得，无法判断。'}\n\n"
            "主要风险\n"
            f"{risk_text or knowledge_text}\n\n"
            "数据边界\n"
            "仅使用已返回的结构化市场数据和可引用知识证据；未返回的行情、估值、财务和公告字段不作推断。"
            "下一步可结合连续报告期趋势、行业可比公司和最新公告继续验证当前判断。"
        )
    return (
        "公司概览\n"
        f"{tool_text}\n\n"
        "公告与知识证据\n"
        f"{knowledge_text}\n\n"
        "待核验事项\n"
        "未返回的行情、估值和财务字段不作推断。"
    )


def _join_claim_sentences(claims: list[str]) -> str:
    normalized = [claim.rstrip("。；; ") for claim in claims if claim.strip()]
    return f"{'；'.join(normalized)}。" if normalized else ""


def _section_claim_text(claims: list[AnswerClaim], allowed_fields: set[str]) -> str:
    selected = [
        claim["text"]
        for claim in claims
        if claim.get("tool_evidence_refs")
        and _claim_field_names(claim) & allowed_fields
    ]
    return _join_claim_sentences(selected)


def _contains_unsupported_numbers(
    *,
    rendered: str,
    claims: list[AnswerClaim],
) -> bool:
    return bool(_unsupported_number_tokens(rendered=rendered, claims=claims))


def _unsupported_number_tokens(
    *,
    rendered: str,
    claims: list[AnswerClaim],
) -> list[str]:
    return [
        str(mention["token"])
        for mention in _number_mentions(rendered)
        if not any(
            _claim_supports_number(mention=mention, claim=claim)
            for claim in claims
        )
    ]


def _claim_supports_number(*, mention: dict, claim: AnswerClaim) -> bool:
    claim_mentions = _number_mentions(claim["text"])
    if mention["kind"] == "date":
        return any(
            candidate["kind"] == "date" and candidate["token"] == mention["token"]
            for candidate in claim_mentions
        )

    fields = _claim_field_names(claim)
    rendered_value, tolerance = _canonical_number(mention=mention, fields=fields)
    for candidate in claim_mentions:
        if candidate["kind"] != "number":
            continue
        claim_value, _ = _canonical_number(mention=candidate, fields=fields)
        if abs(rendered_value - claim_value) <= tolerance:
            return True
    return False


def _canonical_number(*, mention: dict, fields: set[str]) -> tuple[float, float]:
    value = float(mention["value"])
    if value > 0 and mention.get("semantic_negative"):
        value = -value
    unit = str(mention["unit"])
    scale = 1.0
    if unit == "亿" and fields & MONEY_FIELDS:
        scale = 100_000_000.0
    elif unit == "万" and fields & MONEY_FIELDS:
        scale = 10_000.0
    elif unit == "%" and fields & RATIO_FIELDS:
        scale = 0.01
    elif unit == "%" and fields & PERCENT_POINT_FIELDS:
        scale = 1.0

    decimal_places = int(mention["decimal_places"])
    tolerance = 0.5 * (10 ** -decimal_places) * abs(scale)
    if decimal_places == 0:
        unsigned_integer = str(mention["token"]).lstrip("+-").rstrip("%")
        trailing_zeros = len(unsigned_integer) - len(unsigned_integer.rstrip("0"))
        if trailing_zeros:
            coarse_tolerance = min(
                0.5 * (10**trailing_zeros) * abs(scale),
                abs(value * scale) * 0.02,
            )
            tolerance = max(tolerance, coarse_tolerance)
    return value * scale, tolerance + 1e-12


def _number_mentions(value: str) -> list[dict]:
    mentions: list[dict] = []
    for match in NUMBER_PATTERN.finditer(value):
        token = match.group(0)
        line_start = value.rfind("\n", 0, match.start()) + 1
        raw_line_prefix = value[line_start:match.start()]
        suffix = value[match.end():].lstrip()
        unsigned_token = token.lstrip("+-")
        if (
            unsigned_token.isdigit()
            and re.fullmatch(r"[\s#>*_-]*", raw_line_prefix) is not None
            and suffix.startswith((".", "、", ")", "）"))
        ):
            continue
        immediate_prefix = value[max(0, match.start() - 1):match.start()]
        if (
            unsigned_token.isdigit()
            and immediate_prefix in {"(", "（"}
            and suffix.startswith((")", "）"))
        ):
            continue
        if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", token):
            mentions.append({"kind": "date", "token": token})
            continue

        numeric_token = token.rstrip("%")
        decimal_places = (
            len(numeric_token.rsplit(".", maxsplit=1)[1])
            if "." in numeric_token
            else 0
        )
        unit = "%" if token.endswith("%") else ""
        if not unit:
            if suffix.startswith("亿"):
                unit = "亿"
            elif suffix.startswith("万"):
                unit = "万"
        mentions.append(
            {
                "kind": "number",
                "token": token,
                "value": float(numeric_token),
                "decimal_places": decimal_places,
                "unit": unit,
                "approximate": bool(
                    re.search(r"约\s*$", value[max(0, match.start() - 3):match.start()])
                ),
                "semantic_negative": bool(
                    re.search(
                        r"(?:净流出|亏损|为负|负增长|下跌|下降|"
                        r"减少|下滑|回落)[^。！？；;]{0,8}$",
                        value[max(0, match.start() - 20):match.start()],
                    )
                ),
            }
        )
    return mentions


def _remove_unsupported_temporal_context(
    *,
    rendered: str,
    claims: list[AnswerClaim],
) -> str:
    unsupported_years = {
        token
        for token in _unsupported_number_tokens(rendered=rendered, claims=claims)
        if re.fullmatch(r"(?:19|20)\d{2}", token)
    }
    sanitized = rendered
    unsupported_dates = {
        token
        for token in _unsupported_number_tokens(rendered=rendered, claims=claims)
        if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", token)
    }
    for date in unsupported_dates:
        sanitized = re.sub(
            rf"(?:数据截至|截至|基于|按)?[ \t]*{re.escape(date)}",
            "",
            sanitized,
        )
    for year in unsupported_years:
        sanitized = re.sub(
            rf"(?:数据截至|截至|基于|按)?[ \t]*{re.escape(year)}年"
            r"(?:[ \t]*\d{1,2}月(?:[ \t]*\d{1,2}日)?)?(?:末|度)?",
            "",
            sanitized,
        )
    return sanitized


def _contains_required_claim_signals(
    *,
    rendered: str,
    claims: list[AnswerClaim],
) -> bool:
    required_terms = {
        term
        for claim in claims
        if claim.get("tool_evidence_refs")
        for term in _claim_signal_terms(claim["text"])
    }
    if not required_terms:
        return True
    return any(term in rendered for term in required_terms)


def _claim_signal_terms(value: str) -> set[str]:
    terms = set(_number_tokens(value))
    for match in re.finditer(r"[A-Za-z0-9][A-Za-z0-9._%:-]{1,}", value):
        terms.add(match.group(0))
    text = value
    for label in TOOL_FIELD_LABELS.values():
        text = text.replace(label, "")
    text = text.replace("为", "")
    for match in re.finditer(r"[\u4e00-\u9fff]{2,}", text):
        terms.add(match.group(0))
    return {term for term in terms if len(term.strip()) >= 2}


def _number_tokens(value: str) -> set[str]:
    return {str(mention["token"]) for mention in _number_mentions(value)}


def _build_fact_claims(evidence: EvidencePackage) -> list[AnswerClaim]:
    return [
        {
            "claim_id": f"claim_{index + 1}",
            "text": item.content,
            "evidence_ids": [item.evidence_id],
            "confidence": item.score,
            "claim_type": "fact",
        }
        for index, item in enumerate(evidence.items)
    ]


def _render_tool_execution_content(
    *,
    state: AgentState,
    response_policy: dict,
    llm_client: LlmClient | None,
) -> str:
    if not state["tool_calls"]:
        return "没有可用的工具调用结果。"
    latest_call = state["tool_calls"][-1]
    if latest_call.result is None:
        return "工具调用没有返回结果。"
    if latest_call.result.status != "success":
        message = latest_call.result.error.message if latest_call.result.error else "未知错误"
        return f"工具调用失败：{message}"
    result = latest_call.result.result or {}
    fallback = (
        f"已完成：{latest_call.request.purpose}。"
        f"结果：{json.dumps(result, ensure_ascii=False)}"
    )
    if llm_client is None:
        response_policy["tool_result_rendering"] = "fallback"
        return fallback
    try:
        rendered = asyncio.run(
            llm_client.complete(
                system_prompt=(
                    "你是 BsAgent 的工具结果回答渲染器，不代表任何模型厂商。"
                    "把工具执行结果转成面向用户的最终中文回答。"
                    "不得提及 MCP、工具内部名称、DeepSeek、模型名称或 API provider。"
                    "不得新增工具结果之外的事实；如果结果是结构化 JSON，只解释其中已有字段。"
                    "回答要简洁、直接、像产品自己的回答。"
                ),
                user_prompt=json.dumps(
                    {
                        "user_message": state["user_message"],
                        "tool_purpose": latest_call.request.purpose,
                        "tool_result": result,
                        "response_policy": response_policy,
                    },
                    ensure_ascii=False,
                ),
                temperature=0.0,
                max_tokens=256,
                thinking=False,
            )
        )
    except Exception:
        response_policy["tool_result_rendering"] = "failed"
        return fallback
    response_policy["tool_result_rendering"] = "applied"
    response_policy["llm_provider"] = rendered.provider
    response_policy["llm_model"] = rendered.model
    response_policy["llm_usage"] = rendered.usage
    return rendered.content


def _render_direct_response_content(
    *,
    state: AgentState,
    response_policy: dict,
    llm_client: LlmClient | None,
) -> str:
    fallback = _fallback_direct_response_content(state)
    if response_policy.get("response_type") == "approval_rejected_response":
        response_policy["llm_rendering"] = "bypassed_for_control_response"
        return fallback
    if llm_client is None:
        response_policy["llm_rendering"] = "disabled"
        return fallback
    try:
        result = asyncio.run(
            llm_client.complete(
                system_prompt=(
                    "你是 BsAgent 的最终回答渲染器，不代表任何模型厂商。"
                    "不得提及 DeepSeek、模型名称、API provider。"
                    "不得说“我是 DeepSeek”“作为大语言模型”“我是 AI 助手”。"
                    "你只能基于 user_message、intent 和 plan 回答。"
                    "没有执行工具或检索时，不得声称已经执行或查询外部知识库。"
                    "BsAgent 是投研优先的专业助手。投研相关问题应专业回答并引导下一步研究；"
                    "寒暄、产品帮助和简单金融通识可直接回答；明显与投研无关的专业任务"
                    "只简洁说明能力边界，并引导用户回到公司、行业、财务、行情、估值或风险研究。"
                    "回答要符合 BsAgent 风格：专业、直接、中文优先、说明必要边界。"
                ),
                user_prompt=json.dumps(
                    {
                        "user_message": state["user_message"],
                        "intent": state["intent"],
                        "plan": state["plan"],
                        "response_policy": response_policy,
                    },
                    ensure_ascii=False,
                ),
                temperature=0.2,
                max_tokens=384,
                thinking=False,
            )
        )
    except Exception:
        response_policy["llm_rendering"] = "failed"
        return fallback
    response_policy["llm_rendering"] = "applied"
    response_policy["llm_provider"] = result.provider
    response_policy["llm_model"] = result.model
    response_policy["llm_usage"] = result.usage
    return result.content


def _fallback_direct_response_content(state: AgentState) -> str:
    intent = state["intent"] or {}
    plan = state["plan"] or {}
    response_type = plan.get("response_policy", {}).get("response_type", "direct_response")
    if response_type == "conversation_response":
        return f"收到：{state['user_message']}。我在，可以继续说你的目标。"
    if response_type == "clarification_response":
        return f"我需要更多信息才能处理“{state['user_message']}”。请补充目标、对象或约束。"
    if response_type == "tool_unavailable_response":
        return f"我理解你想执行工具操作，但当前没有匹配到可安全调用的工具：{state['user_message']}"
    if response_type == "workflow_not_ready_response":
        return f"我理解这是一个任务流程请求，但当前还没有可执行工作流：{state['user_message']}"
    if response_type == "memory_query_not_ready_response":
        return f"我理解你想查询记忆，但当前记忆查询回答链路还没有完善：{state['user_message']}"
    if response_type == "approval_rejected_response":
        return "该工具操作已被审批人拒绝，未执行任何外部操作。"
    if intent.get("intent") == "unsupported":
        return (
            "这个请求超出了当前投研助手的专业能力边界。"
            "我可以继续协助你研究 A 股公司、行业、财务、行情、估值、公告与风险。"
        )
    return f"我已收到你的请求：{state['user_message']}"


def _draft_confidence(
    *,
    analysis_confidence: float | None,
    claims: list[AnswerClaim],
    coverage: str,
) -> float:
    if not claims:
        return 0.0
    base_confidence = analysis_confidence
    if base_confidence is None:
        base_confidence = max(claim["confidence"] for claim in claims)
    if coverage == "partial":
        return round(base_confidence * 0.5, 2)
    return base_confidence


def _render_content(
    *,
    claims: list[AnswerClaim],
    missing_information: list[str],
    coverage: str,
) -> str:
    claim_sentences = [claim["text"] for claim in claims]
    if coverage == "partial":
        prefix = "根据当前可用证据，目前只能确认："
    else:
        prefix = "根据当前可用证据，可以确认："

    content = prefix + "；".join(claim_sentences)
    if missing_information:
        content += "。仍缺少信息：" + "；".join(missing_information)
    return content


def _build_response_policy(
    *,
    evidence: EvidencePackage | None,
    llm_client: LlmClient | None = None,
) -> dict:
    return {
        "language": "default",
        "format": "concise_answer",
        "citation_style": "inline_title",
        "coverage": evidence.coverage if evidence is not None else "none",
        "uncertainty": "explicit",
        "llm_rendering": "enabled" if llm_client is not None else "disabled",
    }


def _render_with_llm(
    *,
    content: str,
    claims: list[AnswerClaim],
    missing_information: list[str],
    response_policy: dict,
    llm_client: LlmClient | None,
    extra_context: dict | None = None,
    max_tokens: int = 256,
) -> str:
    if llm_client is None:
        return content
    try:
        result = asyncio.run(
            llm_client.complete(
                system_prompt=(
                    "你是 BsAgent 的答案渲染器，不代表任何模型厂商。"
                    "只能根据给定 claims 改写表达，不得新增事实、不得删除引用约束、"
                    "不得编造证据。不得提及 DeepSeek、模型名称或 API provider。"
                ),
                user_prompt=json.dumps(
                    {
                        "draft_content": content,
                        "claims": claims,
                        "missing_information": missing_information,
                        "extra_context": extra_context or {},
                        "response_policy": response_policy,
                    },
                    ensure_ascii=False,
                ),
                temperature=0.0,
                max_tokens=max_tokens,
                thinking=False,
            )
        )
    except Exception:
        response_policy["llm_rendering"] = "failed"
        return content
    response_policy["llm_rendering"] = "applied"
    response_policy["llm_provider"] = result.provider
    response_policy["llm_model"] = result.model
    response_policy["llm_usage"] = result.usage
    return _normalize_brand_voice(result.content)


def _finalize_response_content(content: str) -> str:
    return _normalize_brand_voice(content)


def _normalize_brand_voice(content: str) -> str:
    normalized = content.strip()
    for pattern in FORBIDDEN_BRAND_PATTERNS:
        normalized = re.sub(pattern, "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()
    if not normalized:
        return "我已处理完成，但当前没有可展示的文本结果。"
    return normalized
