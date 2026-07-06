import re
from typing import Any, Literal, cast

from app.schemas.evidence import EvidencePackage
from app.schemas.memory import MemoryContext
from app.schemas.tools import ToolCallRequest
from app.skills.base import ApprovalPolicy, Skill, SkillOutput, SkillSpec
from app.skills.definitions import SkillDefinition, SkillExecutionGuide


RESOLVED_SYMBOL_PLACEHOLDER = "$resolved_symbol"

_COMPANY_QUERY_MARKERS = [
    "帮我看一下",
    "最近有什么风险",
    "分析一下",
    "分析",
    "看看",
]

_SYMBOL_REQUIRED_TOOL_SUFFIXES = (
    ".stock.company_profile",
    ".stock.market_snapshot",
    ".stock.financial_metrics",
    ".stock.announcement_search",
    ".stock.risk_events",
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
            optional_inputs=["evidence", "memory_context"],
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
        memory_context: MemoryContext | None,
    ) -> SkillOutput:
        requested_tools = [
            self._build_tool_request(
                tool_name=tool_name,
                tenant_id=tenant_id,
                user_id=user_id,
                user_message=user_message,
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

    def _build_tool_request(
        self,
        *,
        tool_name: str,
        tenant_id: str,
        user_id: str,
        user_message: str,
    ) -> ToolCallRequest:
        arguments = self._tool_arguments(
            tool_name=tool_name,
            tenant_id=tenant_id,
            user_id=user_id,
            user_message=user_message,
        )
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
        if tool_name.endswith(".stock.financial_metrics"):
            return {
                "symbol": RESOLVED_SYMBOL_PLACEHOLDER,
                "period": _period_from_message(user_message),
            }
        if tool_name.endswith(".stock.announcement_search"):
            return {"symbol": RESOLVED_SYMBOL_PLACEHOLDER, "date_range": "12m"}
        if tool_name.endswith(".stock.risk_events"):
            return {"symbol": RESOLVED_SYMBOL_PLACEHOLDER, "date_range": "6m"}
        if tool_name == "rag_search":
            return {
                "query": user_message,
                "tenant_id": tenant_id,
                "user_id": user_id,
                "top_k": 8,
                "retrieval_mode": "hybrid",
            }
        return {"query": user_message}


def _company_query(user_message: str) -> str:
    query = user_message.strip()
    for marker in _COMPANY_QUERY_MARKERS:
        query = query.replace(marker, "")
    query = re.sub(r"20\d{2}\s*年?\s*(?:Q[1-4]|[一二三四]季报|半年报|年报)?", "", query)
    query = re.sub(r"\s+", " ", query)
    return query.strip(" ，。？！?") or user_message.strip()


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
