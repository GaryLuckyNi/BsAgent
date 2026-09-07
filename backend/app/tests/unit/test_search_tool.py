import asyncio

from app.schemas.tools import ToolCallRequest
from app.tools.execution_gateway import ToolExecutionContext, ToolExecutionGateway
from app.tools.registry import ToolRegistry
from app.tools.search import ToolSearchHit, ToolSearchResponse
from app.tools.search.index import ToolSearchUnavailableError
from app.tools.search.tool import SearchTool


class _RecordingSearchService:
    def __init__(self):
        self.context = None

    def search(self, request, *, context):
        self.context = context
        return ToolSearchResponse(
            catalog_digest="catalog-1",
            mode="exact",
            hits=[
                ToolSearchHit(
                    tool_name="mcp.tushare.income",
                    tool_version="1.0.0",
                    description="查询利润表",
                    rank=1,
                    sources=["exact"],
                )
            ],
        )


def test_gateway_passes_trusted_context_to_local_search_tool():
    service = _RecordingSearchService()
    registry = ToolRegistry()
    registry.register(SearchTool(service))
    result = asyncio.run(
        ToolExecutionGateway(registry.snapshot()).execute(
            ToolCallRequest(
                tool_name="search_tool",
                arguments={"query": "利润表", "top_k": 5},
                purpose="发现利润表工具",
            ),
            trace_id="trace-1",
            context=ToolExecutionContext(
                tenant_id="tenant-1",
                user_id="user-1",
                session_id="session-1",
                allowed_tools=("search_tool",),
                permission_scopes=("market:financial",),
            ),
        )
    )

    assert result.status == "success"
    assert result.result["hits"][0]["tool_name"] == "mcp.tushare.income"
    assert service.context.tenant_id == "tenant-1"
    assert service.context.permission_scopes == ("market:financial",)


def test_search_tool_rejects_calls_without_gateway_context():
    tool = SearchTool(_RecordingSearchService())

    try:
        asyncio.run(tool.run(tool.input_model(query="利润表")))
    except RuntimeError as exc:
        assert "Gateway" in str(exc)
    else:
        raise AssertionError("search_tool must reject direct execution")


def test_gateway_preserves_search_unavailable_error_code():
    class _UnavailableService:
        def search(self, request, *, context):
            raise ToolSearchUnavailableError()

    registry = ToolRegistry()
    registry.register(SearchTool(_UnavailableService()))

    result = asyncio.run(
        ToolExecutionGateway(registry.snapshot()).execute(
            ToolCallRequest(
                tool_name="search_tool",
                arguments={"query": "利润表"},
                purpose="发现工具",
            ),
            trace_id="trace-1",
            context=ToolExecutionContext(
                tenant_id="tenant-1",
                user_id="user-1",
                session_id="session-1",
                allowed_tools=("search_tool",),
            ),
        )
    )

    assert result.status == "failed"
    assert result.error.code == "tool_search_unavailable"
