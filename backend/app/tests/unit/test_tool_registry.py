import asyncio

import pytest
from pydantic import BaseModel

from app.schemas.tools import ToolCallRequest, ToolCallResult
from app.tools.agent import ToolAgentExecutor
from app.tools.base import Tool
from app.tools.execution_gateway import ToolExecutionContext
from app.tools.registry import ToolRegistry


class MutableInput(BaseModel):
    value: str


class MutableTool(Tool[MutableInput]):
    name = "mutable_tool"
    version = "1.0.0"
    input_model = MutableInput
    risk_level = "high"
    permission_scope = ["mutable:run"]

    async def run(self, input_data: MutableInput) -> ToolCallResult:
        return ToolCallResult(
            tool_call_id="tool_mutable",
            tool_name=self.name,
            status="success",
            result={"value": input_data.value},
            trace_id="trace_tool",
        )


def test_registry_snapshot_freezes_spec_but_keeps_executor_callable():
    tool = MutableTool()
    registry = ToolRegistry()
    registry.register(tool)
    snapshot = registry.snapshot()

    tool.risk_level = "low"
    tool.permission_scope.append("mutated:scope")
    exposed_spec = snapshot.get("mutable_tool").spec
    exposed_spec.risk_level = "low"
    exposed_spec.permission_scope.append("caller:mutation")

    frozen_spec = snapshot.get("mutable_tool").spec
    assert frozen_spec.risk_level == "high"
    assert frozen_spec.permission_scope == ["mutable:run"]

    context = ToolExecutionContext(
        tenant_id="tenant_1",
        user_id="user_1",
        session_id="session_1",
        allowed_tools=("mutable_tool",),
        permission_scopes=("mutable:run",),
    )
    denied = asyncio.run(
        ToolAgentExecutor(registry=snapshot).execute(
            ToolCallRequest(
                tool_name="mutable_tool",
                arguments={"value": "hello"},
                purpose="验证冻结风险目录",
            ),
            trace_id="trace_1",
            context=context,
        )
    )
    assert denied.status == "denied"
    assert denied.error.code == "tool_trusted_call_id_required"

    with pytest.raises(RuntimeError, match="frozen"):
        snapshot.register(MutableTool())


def test_tool_catalog_digest_changes_for_same_version_security_contract_change():
    first = ToolRegistry()
    first.register(MutableTool())

    changed_tool = MutableTool()
    changed_tool.risk_level = "low"
    changed_tool.output_schema = {"type": "object", "required": ["changed"]}
    second = ToolRegistry()
    second.register(changed_tool)

    assert first.catalog_digest != second.catalog_digest
