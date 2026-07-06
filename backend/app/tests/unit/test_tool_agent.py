import asyncio

from pydantic import BaseModel

from app.schemas.tools import ToolCallRequest, ToolCallResult
from app.tools.agent import ToolAgentExecutor
from app.tools.base import Tool
from app.tools.registry import ToolRegistry


class EchoInput(BaseModel):
    text: str


class EchoTool(Tool[EchoInput]):
    name = "echo"
    version = "1.0.0"
    input_model = EchoInput

    async def run(self, input_data: EchoInput) -> ToolCallResult:
        return ToolCallResult(
            tool_call_id="tool_echo",
            tool_name=self.name,
            status="success",
            result={"text": input_data.text},
            latency_ms=1,
            trace_id="trace_tool",
        )


def _executor() -> ToolAgentExecutor:
    registry = ToolRegistry()
    registry.register(EchoTool())
    return ToolAgentExecutor(registry=registry)


def test_tool_agent_executes_registered_tool_with_validated_arguments():
    result = asyncio.run(
        _executor().execute(
            ToolCallRequest(
                tool_name="echo",
                tool_version="1.0.0",
                arguments={"text": "hello"},
                purpose="验证工具代理执行路径",
            ),
            trace_id="trace_1",
        )
    )

    assert result.status == "success"
    assert result.result == {"text": "hello"}
    assert result.tool_name == "echo"


def test_tool_agent_rejects_unknown_tool():
    result = asyncio.run(
        _executor().execute(
            ToolCallRequest(
                tool_name="missing",
                arguments={},
                purpose="验证未知工具被拒绝",
            ),
            trace_id="trace_1",
        )
    )

    assert result.status == "failed"
    assert result.error.code == "tool_not_found"


def test_tool_agent_rejects_tool_version_mismatch():
    result = asyncio.run(
        _executor().execute(
            ToolCallRequest(
                tool_name="echo",
                tool_version="2.0.0",
                arguments={"text": "hello"},
                purpose="验证工具版本冲突被拒绝",
            ),
            trace_id="trace_1",
        )
    )

    assert result.status == "failed"
    assert result.error.code == "tool_version_mismatch"


def test_tool_agent_rejects_invalid_arguments_before_execution():
    result = asyncio.run(
        _executor().execute(
            ToolCallRequest(
                tool_name="echo",
                arguments={"unknown": "hello"},
                purpose="验证参数 schema 拦截",
            ),
            trace_id="trace_1",
        )
    )

    assert result.status == "failed"
    assert result.error.code == "tool_argument_validation_failed"


def test_tool_agent_denies_approval_required_call_before_execution():
    result = asyncio.run(
        _executor().execute(
            ToolCallRequest(
                tool_name="echo",
                arguments={"text": "hello"},
                purpose="验证审批闸门",
                approval_required=True,
            ),
            trace_id="trace_1",
        )
    )

    assert result.status == "denied"
    assert result.error.code == "tool_approval_required"
