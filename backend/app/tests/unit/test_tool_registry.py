import asyncio

import pytest
from pydantic import BaseModel

from app.schemas.tools import RetryPolicy, ToolCallRequest, ToolCallResult
from app.tools.agent import ToolAgentExecutor
from app.tools.base import Tool
from app.tools.execution_gateway import ToolExecutionContext
from app.tools.registry import ExecutionBinding, ToolRegistry


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


def test_catalog_digest_covers_complete_spec_and_execution_binding():
    class ContractTool(MutableTool):
        @property
        def spec(self):
            return super().spec.model_copy(
                update={"retry_policy": RetryPolicy(max_attempts=3, backoff_ms=250)}
            )

    first_tool = ContractTool()
    first = ToolRegistry()
    first.register(
        first_tool,
        execution_binding=ExecutionBinding(
            provider="mcp",
            implementation_id="mcp.mock.mutable_tool",
            server_id="mock-a",
            deployment_revision="deploy-1",
            config_revision="config-1",
        ),
    )

    changed_timeout = ContractTool()
    changed_timeout.timeout_ms = first_tool.timeout_ms + 1
    second = ToolRegistry()
    second.register(
        changed_timeout,
        execution_binding=ExecutionBinding(
            provider="mcp",
            implementation_id="mcp.mock.mutable_tool",
            server_id="mock-a",
            deployment_revision="deploy-1",
            config_revision="config-1",
        ),
    )

    class ChangedRetryTool(ContractTool):
        @property
        def spec(self):
            return super().spec.model_copy(
                update={"retry_policy": RetryPolicy(max_attempts=4, backoff_ms=250)}
            )

    changed_retry = ChangedRetryTool()
    third = ToolRegistry()
    third.register(
        changed_retry,
        execution_binding=ExecutionBinding(
            provider="mcp",
            implementation_id="mcp.mock.mutable_tool",
            server_id="mock-a",
            deployment_revision="deploy-1",
            config_revision="config-1",
        ),
    )

    changed_binding = ToolRegistry()
    changed_binding.register(
        ContractTool(),
        execution_binding=ExecutionBinding(
            provider="mcp",
            implementation_id="mcp.mock.mutable_tool",
            server_id="mock-b",
            deployment_revision="deploy-2",
            config_revision="config-2",
        ),
    )

    assert first.catalog_digest != second.catalog_digest
    assert first.catalog_digest != third.catalog_digest
    assert first.catalog_digest != changed_binding.catalog_digest
    assert first.get("mutable_tool").execution_binding_digest != changed_binding.get(
        "mutable_tool"
    ).execution_binding_digest


def test_external_tool_registration_requires_explicit_execution_binding():
    tool = MutableTool()
    tool.requires_explicit_execution_binding = True

    with pytest.raises(ValueError, match="execution_binding"):
        ToolRegistry().register(tool)


def test_local_tool_binding_is_stable_and_frozen_with_snapshot():
    registry = ToolRegistry(build_revision="build-42")
    registry.register(MutableTool())
    snapshot = registry.snapshot()

    entry = snapshot.get("mutable_tool")
    assert entry.execution_binding.provider == "local"
    assert entry.execution_binding.implementation_id.endswith(".MutableTool")
    assert entry.execution_binding.deployment_revision == "build-42"
    assert entry.execution_binding_digest == snapshot.get(
        "mutable_tool"
    ).execution_binding_digest
