from typing import Protocol

from app.schemas.tools import ToolCallResult
from app.tools.base import Tool
from app.tools.execution_gateway import ToolExecutionContext
from app.tools.search.contracts import (
    ToolSearchContext,
    ToolSearchInput,
    ToolSearchResponse,
)


class ToolSearchProvider(Protocol):
    def search(
        self,
        request: ToolSearchInput,
        *,
        context: ToolSearchContext,
    ) -> ToolSearchResponse: ...


class SearchTool(Tool[ToolSearchInput]):
    name = "search_tool"
    version = "1.0.0"
    input_model = ToolSearchInput
    description = "按当前任务搜索可用工具；仅在已披露工具不足时调用。"
    output_schema = ToolSearchResponse.model_json_schema()
    risk_level = "low"

    def __init__(self, service: ToolSearchProvider) -> None:
        self._service = service

    async def run_with_context(
        self,
        input_data: ToolSearchInput,
        *,
        context: ToolExecutionContext,
    ) -> ToolCallResult:
        response = self._service.search(
            input_data,
            context=ToolSearchContext(
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                permission_scopes=context.permission_scopes,
                skill_allowed_tools=context.skill_allowed_tools,
            ),
        )
        return ToolCallResult(
            tool_call_id="",
            tool_name=self.name,
            status="success",
            result=response.model_dump(mode="json"),
            trace_id="",
        )

    async def run(self, input_data: ToolSearchInput) -> ToolCallResult:
        raise RuntimeError("search_tool 必须由 ToolExecutionGateway 注入上下文。")
