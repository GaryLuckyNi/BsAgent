import re
from typing import Any, Literal, cast

from app.schemas.evidence import EvidencePackage
from app.schemas.tools import ToolCallRequest
from app.skills.base import ApprovalPolicy, Skill, SkillOutput, SkillSpec
from app.skills.definitions import SkillDefinition, SkillExecutionGuide


RESOLVED_SYMBOL_PLACEHOLDER = "$resolved_symbol"

_COMPANY_QUERY_MARKERS = [
    "请帮我",
    "帮我",
    "帮我看一下",
    "最近有什么风险",
    "近期有什么风险",
    "有什么风险",
    "分析一下",
    "分析下",
    "分析",
    "看看",
    "看一下",
    "研究一下",
]

_COMPANY_QUERY_TOPIC_MARKERS = [
    "技术指标",
    "财务指标",
    "投研分析",
    "公司分析",
    "公司概览",
    "公司速览",
    "分析报告",
    "基本面",
    "最新行情",
    "市场表现",
    "股价表现",
    "K线",
    "k线",
    "走势",
    "趋势",
    "行情",
    "股价",
    "风险",
    "财报",
    "年报",
    "半年报",
    "季报",
]

_A_SHARE_CODE_PATTERN = re.compile(
    r"(?<!\d)(?P<code>[0368]\d{5})(?:\.(?P<exchange>SH|SZ|BJ))?(?!\d)",
    re.IGNORECASE,
)

_SYMBOL_REQUIRED_TOOL_SUFFIXES = (
    ".stock.company_profile",
    ".stock.market_snapshot",
    ".stock.price_trend",
    ".stock.financial_metrics",
    ".stock.announcement_search",
)

_MCP_MARKET_TOOL_PATTERN = re.compile(
    r"^mcp\..+\.(?P<capability>stock\.[a-z0-9_]+)$"
)


class ConfiguredSkill(Skill):
    def __init__(
        self,
        *,
        definition: SkillDefinition,
        guide: SkillExecutionGuide,
        risk_level: str = "medium",
    ) -> None:
        self.definition = definition
        self.guide = guide
        _validate_tool_dependencies(definition.allowed_tools)
        self.spec = SkillSpec(
            name=definition.name,
            version=definition.version,
            description=definition.description,
            supported_intents=definition.input_contract.get("supported_intents", []),
            required_inputs=["user_message"],
            optional_inputs=["evidence"],
            output_schema=definition.output_contract,
            risk_level=cast(Literal["low", "medium", "high"], risk_level),
            approval_policy=ApprovalPolicy(require_human=risk_level == "high"),
            allowed_tools=definition.allowed_tools,
        )

    def run(
        self,
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
        user_message: str,
        context: dict[str, Any],
        evidence: EvidencePackage | None,
    ) -> SkillOutput:
        evidence_contract_failure = self._evidence_contract_failure()
        if evidence_contract_failure is not None:
            return SkillOutput(
                skill_name=self.spec.name,
                skill_version=self.spec.version,
                status="failed",
                requested_tools=[],
                draft_result={
                    "definition_hash": self.definition.definition_hash,
                    "context_intent": context.get("intent", {}),
                },
                reason_codes=[evidence_contract_failure],
            )

        requested_tools = [
            self._build_tool_request(
                tool_name=tool_name,
                tenant_id=tenant_id,
                user_id=user_id,
                user_message=user_message,
                context=context,
            )
            for tool_name in self.definition.allowed_tools
        ]
        has_rag_search = any(
            request.tool_name == "rag_search" for request in requested_tools
        )
        return SkillOutput(
            skill_name=self.spec.name,
            skill_version=self.spec.version,
            status="needs_retrieval" if has_rag_search else "success",
            requested_tools=requested_tools,
            draft_result={
                "definition_hash": self.definition.definition_hash,
                "guide_hash": self.guide.definition_hash,
                "context_intent": context.get("intent", {}),
            },
            reason_codes=["configured_skill_from_registry"],
        )

    def _evidence_contract_failure(self) -> str | None:
        market_capabilities = [
            capability
            for tool_name in self.definition.allowed_tools
            if (capability := _market_capability(tool_name)) is not None
        ]
        if not market_capabilities:
            return None
        if self.definition.evidence_contract_error is not None:
            return "skill_evidence_contract_invalid"

        requirements = {
            requirement.capability: requirement
            for requirement in self.definition.evidence_contract.requirements
        }
        if len(market_capabilities) != len(set(market_capabilities)):
            return "skill_evidence_contract_invalid"
        extra_capabilities = set(requirements) - set(market_capabilities)
        if extra_capabilities:
            return "skill_evidence_contract_invalid"
        if any(capability not in requirements for capability in market_capabilities):
            return "skill_evidence_contract_missing"
        return None

    def _build_tool_request(
        self,
        *,
        tool_name: str,
        tenant_id: str,
        user_id: str,
        user_message: str,
        context: dict[str, Any],
    ) -> ToolCallRequest:
        enriched_message = _message_with_image_entities(
            user_message=user_message,
            context=context,
        )
        arguments = self._tool_arguments(
            tool_name=tool_name,
            tenant_id=tenant_id,
            user_id=user_id,
            user_message=enriched_message,
        )
        capability = _market_capability(tool_name)
        if capability is not None:
            requirement = next(
                requirement
                for requirement in self.definition.evidence_contract.requirements
                if requirement.capability == capability
            )
            arguments = {
                **arguments,
                "required_fields": list(requirement.required_fields),
                "optional_fields": list(requirement.optional_fields),
            }
        return ToolCallRequest(
            tool_name=tool_name,
            tool_version="1.0.0" if tool_name == "rag_search" else None,
            arguments=arguments,
            purpose=f"执行配置 Skill {self.spec.name} 的工具调用",
            approval_required=self.spec.approval_policy.require_human,
        )

    def _tool_arguments(
        self,
        *,
        tool_name: str,
        tenant_id: str,
        user_id: str,
        user_message: str,
    ) -> dict[str, Any]:
        if tool_name.endswith(".stock.resolve_company"):
            return {"query": _company_query(user_message)}
        if tool_name.endswith(".stock.company_profile"):
            return {"symbol": RESOLVED_SYMBOL_PLACEHOLDER}
        if tool_name.endswith(".stock.market_snapshot"):
            return {"symbol": RESOLVED_SYMBOL_PLACEHOLDER}
        if tool_name.endswith(".stock.price_trend"):
            return {"symbol": RESOLVED_SYMBOL_PLACEHOLDER, "window_days": 120}
        if tool_name.endswith(".stock.financial_metrics"):
            return {
                "symbol": RESOLVED_SYMBOL_PLACEHOLDER,
                "period": _period_from_message(user_message),
            }
        if tool_name.endswith(".stock.announcement_search"):
            return {"symbol": RESOLVED_SYMBOL_PLACEHOLDER, "date_range": "12m"}
        if tool_name == "rag_search":
            return {
                "query": user_message,
                "tenant_id": tenant_id,
                "user_id": user_id,
                "top_k": 8,
                "retrieval_mode": "hybrid",
            }
        return {"query": user_message}


def _market_capability(tool_name: str) -> str | None:
    match = _MCP_MARKET_TOOL_PATTERN.fullmatch(tool_name)
    return match.group("capability") if match is not None else None


def _company_query(user_message: str) -> str:
    return extract_company_query(user_message)


def extract_company_query(user_message: str) -> str:
    code_match = _A_SHARE_CODE_PATTERN.search(user_message)
    if code_match is not None:
        code = code_match.group("code")
        exchange = code_match.group("exchange")
        return f"{code}.{exchange.upper()}" if exchange else code

    query = user_message.strip()
    for marker in _COMPANY_QUERY_MARKERS:
        query = query.replace(marker, "")
    query = re.sub(
        r"20\d{2}\s*(?:年\s*)?(?:Q[1-4]|[一二三四]季报|半年报|年报|年度报告|中报|报)?",
        "",
        query,
        flags=re.IGNORECASE,
    )
    for marker in _COMPANY_QUERY_TOPIC_MARKERS:
        query = query.replace(marker, "")
    query = re.sub(r"[，,。？！?；;：:（）()【】\[\]《》<>]", " ", query)
    query = re.sub(r"\s+", " ", query)
    return query.strip() or user_message.strip()


def _message_with_image_entities(
    *,
    user_message: str,
    context: dict[str, Any],
) -> str:
    entities = list(
        dict.fromkeys(
            entity
            for analysis in context.get("image_analyses", [])
            for entity in analysis.get("entities", [])
            if isinstance(entity, str) and entity.strip()
        )
    )
    if not entities:
        return user_message
    entity_text = " ".join(entities)
    if any(marker in user_message for marker in ("这张图", "图片", "截图")):
        return entity_text
    missing_entities = [entity for entity in entities if entity not in user_message]
    if not missing_entities:
        return user_message
    return f"{user_message} {' '.join(missing_entities)}"


def _period_from_message(user_message: str) -> str:
    year_match = re.search(r"(20\d{2})", user_message)
    year = year_match.group(1) if year_match else "2024"

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


def _validate_tool_dependencies(allowed_tools: list[str]) -> None:
    resolver_index = next(
        (
            index
            for index, tool_name in enumerate(allowed_tools)
            if tool_name.endswith(".stock.resolve_company")
        ),
        None,
    )
    for index, tool_name in enumerate(allowed_tools):
        if not tool_name.endswith(_SYMBOL_REQUIRED_TOOL_SUFFIXES):
            continue
        if resolver_index is None:
            raise ValueError(
                f"Configured skill tool {tool_name} requires a stock.resolve_company tool."
            )
        if resolver_index > index:
            raise ValueError(
                f"Configured skill tool {tool_name} requires stock.resolve_company before it."
            )
