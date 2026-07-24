import asyncio
import json
import re

from app.graph.state import AgentState, AnswerClaim
from app.llm.factory import LlmClient
from app.schemas.evidence import EvidencePackage
from app.schemas.memory import MemoryContext


FORBIDDEN_BRAND_PATTERNS = (
    r"我是\s*DeepSeek[^\n。]*[。]?",
    r"作为\s*DeepSeek[^\n。]*[。]?",
    r"作为一个?大语言模型[^\n。]*[。]?",
    r"我是一个?AI助手[^\n。]*[。]?",
    r"DeepSeek",
)

FINANCIAL_DISCLAIMER = "以上内容仅用于投研辅助分析，不构成投资建议、买卖建议或收益承诺。"


def build_synthesizer_node(*, llm_client: LlmClient | None = None):
    def node(state: AgentState) -> AgentState:
        evidence = state["evidence"]
        analysis = state["analysis"] or {}
        plan = state["plan"] or {}
        plan_response_policy = plan.get("response_policy", {})
        missing_information = analysis.get("missing_information", [])
        response_policy = _build_response_policy(
            evidence=evidence,
            memory_context=state["memory_context"],
            llm_client=llm_client,
        )
        response_policy.update(plan_response_policy)

        handled_error = next(
            (error for error in reversed(state["errors"]) if error.get("handled")),
            None,
        )
        if handled_error is not None:
            response_policy["response_type"] = "execution_error_response"
            state["draft_answer"] = {
                "content": _finalize_response_content(
                    handled_error["message"],
                    state["memory_context"],
                ),
                "claims": [],
                "cited_evidence_ids": [],
                "missing_information": [handled_error["code"]],
                "assumptions": [],
                "confidence": 0.0,
                "response_policy": response_policy,
            }
            return state

        if plan.get("execution_mode") == "direct_response":
            content = _render_direct_response_content(
                state=state,
                response_policy=response_policy,
                llm_client=llm_client,
            )
            state["draft_answer"] = {
                "content": _finalize_response_content(content, state["memory_context"]),
                "claims": [],
                "cited_evidence_ids": [],
                "missing_information": missing_information,
                "assumptions": [],
                "confidence": (state["intent"] or {}).get("confidence", 0.8),
                "response_policy": response_policy,
            }
            return state
        if plan.get("execution_mode") == "tool_execution":
            content = _render_tool_execution_content(
                state=state,
                response_policy=response_policy,
                llm_client=llm_client,
            )
            state["draft_answer"] = {
                "content": _finalize_response_content(content, state["memory_context"]),
                "claims": [],
                "cited_evidence_ids": [],
                "missing_information": [] if state["tool_calls"] else ["没有工具调用结果"],
                "assumptions": [],
                "confidence": 1.0 if state["tool_calls"] else 0.0,
                "response_policy": response_policy,
            }
            return state

        if response_policy.get("response_type", "").startswith("equity_research_"):
            content = _render_equity_research_content(
                state=state,
                response_policy=response_policy,
            )
            state["draft_answer"] = {
                "content": _finalize_response_content(content, state["memory_context"]),
                "claims": [],
                "cited_evidence_ids": [
                    item.evidence_id for item in evidence.items
                ] if evidence is not None else [],
                "missing_information": missing_information,
                "assumptions": [],
                "confidence": analysis.get("confidence", 0.6),
                "response_policy": response_policy,
            }
            return state

        if evidence is None or not evidence.items:
            content = "当前知识库没有检索到足够证据，无法给出可靠结论。"
            state["draft_answer"] = {
                "content": _finalize_response_content(content, state["memory_context"]),
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
            "content": _finalize_response_content(rendered_content, state["memory_context"]),
            "claims": claims,
            "cited_evidence_ids": cited_evidence_ids,
            "missing_information": missing_information,
            "assumptions": [],
            "confidence": confidence,
            "response_policy": response_policy,
        }
        return state

    return node


def synthesizer_node(state: AgentState) -> AgentState:
    return build_synthesizer_node()(state)


def _render_equity_research_content(*, state: AgentState, response_policy: dict) -> str:
    analysis = state["analysis"] or {}
    intent = state["intent"] or {}
    structured = analysis.get("structured_tool_results", [])
    findings = analysis.get("findings", [])
    profile = _first_stock_tool_result(structured, "company_profile")
    metrics = _first_stock_tool_result(structured, "financial_metrics")
    risks = _first_stock_tool_result(structured, "risk_events")
    response_type = response_policy.get("response_type")
    if response_type == "equity_research_financial_report_analysis":
        body = _financial_report_template(
            profile=profile,
            metrics=metrics,
            findings=findings,
        )
    elif response_type == "equity_research_risk_scan":
        body = _risk_scan_template(profile=profile, risks=risks, findings=findings)
    else:
        body = _company_snapshot_template(
            profile=profile,
            metrics=metrics,
            findings=findings,
        )
    if intent.get("domain") == "a_share_equity_research" and FINANCIAL_DISCLAIMER not in body:
        body = f"{body}\n\n免责声明\n{FINANCIAL_DISCLAIMER}"
    return body


def _first_stock_tool_result(structured: list[dict], suffix: str) -> dict:
    for item in structured:
        if item.get("tool_name", "").endswith(suffix):
            return item.get("result") or {}
    return {}


def _company_snapshot_template(*, profile: dict, metrics: dict, findings: list[str]) -> str:
    name = profile.get("name") or "该公司"
    business = profile.get("main_business") or "当前结构化资料未返回主营业务。"
    industry = profile.get("industry") or "未返回行业分类"
    evidence_text = "；".join(findings[:3]) if findings else "当前 RAG 未返回足够正文证据。"
    metric_payload = metrics.get("metrics", {}) if metrics else "当前未返回结构化财务指标。"
    return (
        "公司概览\n"
        f"{name}，行业：{industry}。\n\n"
        "核心业务\n"
        f"{business}\n\n"
        "关键财务表现\n"
        f"{metric_payload}\n\n"
        "近期重要事件\n"
        f"{evidence_text}\n\n"
        "主要风险点\n"
        "需要结合公告、财报和行业景气度继续验证。\n\n"
        "待进一步验证的问题\n"
        "后续应继续核对最新公告、财务指标变化和行业数据。"
    )


def _financial_report_template(*, profile: dict, metrics: dict, findings: list[str]) -> str:
    metric_payload = metrics.get("metrics", {}) if metrics else {}
    evidence_text = "；".join(findings[:3]) if findings else "当前 RAG 未返回足够财报正文证据。"
    return (
        "摘要结论\n"
        f"{profile.get('name', '该公司')} 财报分析需要结合结构化指标和公告正文。\n\n"
        "收入与利润变化\n"
        f"{metric_payload}\n\n"
        "盈利质量\n"
        "重点关注毛利率、ROE 和扣非利润变化。\n\n"
        "现金流质量\n"
        "重点关注经营现金流与净利润的匹配度。\n\n"
        "异常点\n"
        f"{evidence_text}"
    )


def _risk_scan_template(*, profile: dict, risks: dict, findings: list[str]) -> str:
    events = risks.get("events", []) if risks else []
    evidence_text = "；".join(findings[:3]) if findings else "当前 RAG 未返回足够风险证据。"
    return (
        "风险总览\n"
        f"{profile.get('name', '该公司')} 当前结构化风险事件数量：{len(events)}。\n\n"
        "经营风险\n"
        f"{events}\n\n"
        "监管/合规风险\n"
        f"{evidence_text}\n\n"
        "后续关注点\n"
        "继续跟踪公告、问询函、处罚、诉讼、减持和质押变化。"
    )


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
                    "你只能基于 user_message、intent、plan 和 memory_context 回答。"
                    "没有执行工具或检索时，不得声称已经执行或查询外部知识库。"
                    "回答要符合 BsAgent 风格：专业、直接、中文优先、说明必要边界。"
                ),
                user_prompt=json.dumps(
                    {
                        "user_message": state["user_message"],
                        "intent": state["intent"],
                        "plan": state["plan"],
                        "memory_context": _memory_context_payload(state["memory_context"]),
                        "response_policy": response_policy,
                    },
                    ensure_ascii=False,
                ),
                temperature=0.2,
                max_tokens=384,
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
        return f"当前我还不能可靠处理这个请求：{state['user_message']}"
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
    memory_context: MemoryContext | None,
    llm_client: LlmClient | None = None,
) -> dict:
    return {
        "language": _preferred_language(memory_context),
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
                        "response_policy": response_policy,
                    },
                    ensure_ascii=False,
                ),
                temperature=0.0,
                max_tokens=256,
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


def _preferred_language(memory_context: MemoryContext | None) -> str:
    if memory_context is None:
        return "default"
    joined_entries = "\n".join(memory_context.entries)
    if any(marker in joined_entries for marker in ("用中文", "中文回答", "中文文档")):
        return "zh"
    return "default"


def _apply_memory_preferences(content: str, memory_context: MemoryContext | None) -> str:
    if memory_context is None:
        return content
    if _preferred_language(memory_context) == "zh":
        return f"按你的中文偏好回答：{content}"
    return content


def _finalize_response_content(content: str, memory_context: MemoryContext | None) -> str:
    return _apply_memory_preferences(_normalize_brand_voice(content), memory_context)


def _normalize_brand_voice(content: str) -> str:
    normalized = content.strip()
    for pattern in FORBIDDEN_BRAND_PATTERNS:
        normalized = re.sub(pattern, "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()
    if not normalized:
        return "我已处理完成，但当前没有可展示的文本结果。"
    return normalized


def _memory_context_payload(memory_context: MemoryContext | None) -> dict:
    if memory_context is None:
        return {"entries": [], "sources": []}
    return {
        "entries": memory_context.entries,
        "sources": memory_context.sources,
    }
