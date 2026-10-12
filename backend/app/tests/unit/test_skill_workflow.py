import json

import pytest
from app.llm.schemas import LlmCompletionResult
from app.skills.definitions import SkillDefinition
from app.skills.workflow import WorkflowError, compile_workflow, missing_fields
from app.tools.base import Tool
from app.tools.named_lookup import resolve_named_tool
from app.tools.registry import ToolRegistry
from pydantic import BaseModel


class Input(BaseModel):
    value: str


class ExampleTool(Tool):
    name = "mcp.local.first"
    version = "1"
    input_model = Input

    async def run(self, data):
        raise AssertionError("编译时不能执行工具")


def registry(*names):
    result = ToolRegistry()
    for name in names or ("mcp.local.first",):
        tool = ExampleTool()
        tool.name = name
        result.register(tool)
    return result


class CompilerLlm:
    def __init__(self, steps, output_requirement=""):
        self.steps = steps
        self.output_requirement = output_requirement
        self.calls = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        return LlmCompletionResult(
            content=json.dumps(
                {"steps": self.steps, "output_requirement": self.output_requirement}
            ),
            model="fake",
            provider="fake",
        )


def step(**changes):
    return {
        "instruction": "调用 first 获取 value",
        "source": "调用 first，必须返回 value。",
        "tool_name": "first",
        "required_fields": ["value"],
        "completion_criteria": [],
        **changes,
    }


def test_named_tool_loads_full_schema_without_semantic_selection():
    spec = resolve_named_tool(registry("mcp.local.first"), "first", permission_scopes=())
    assert spec.name == "mcp.local.first"
    assert spec.input_schema["required"] == ["value"]


def test_named_tool_rejects_missing_and_ambiguous_names():
    with pytest.raises(WorkflowError, match="不存在"):
        resolve_named_tool(registry(), "absent", permission_scopes=())
    with pytest.raises(WorkflowError, match="多个"):
        resolve_named_tool(registry("mcp.a.first", "mcp.b.first"), "first", permission_scopes=())
    assert (
        resolve_named_tool(
            registry("mcp.a.first", "mcp.b.first"), "mcp.a.first", permission_scopes=()
        ).name
        == "mcp.a.first"
    )


def test_compiler_freezes_definition_and_only_resolves_explicit_tool():
    definition = SkillDefinition(
        name="skill",
        version="1",
        description="调用 first，必须返回 value。用表格输出。",
        definition_hash="hash",
    )
    workflow = compile_workflow(
        definition=definition,
        parent_task_id="parent",
        llm_client=CompilerLlm([step()]),
        registry=registry(),
        permission_scopes=(),
    )
    assert workflow.summary == definition.description
    assert workflow.definition_hash == "hash"
    assert workflow.steps[0].tool_name == "mcp.local.first"
    assert workflow.steps[0].tool_version == "1"
    assert workflow.steps[0].task_id.startswith("sw_")


@pytest.mark.parametrize(
    "draft",
    [step(tool_name="unmentioned"), step(source="伪造步骤"), step(required_fields=["invented"])],
)
def test_compiler_rejects_invented_constraints(draft):
    definition = SkillDefinition(
        name="skill",
        version="1",
        description="调用 first，必须返回 value。",
        definition_hash="hash",
    )
    with pytest.raises(WorkflowError):
        compile_workflow(
            definition=definition,
            parent_task_id="parent",
            llm_client=CompilerLlm([draft]),
            registry=registry("unmentioned", "mcp.local.first"),
            permission_scopes=(),
        )


def test_required_fields_accept_zero_but_reject_empty_and_missing():
    assert missing_fields({"rows": [{"pay": 0}, {"pay": None}]}, ["rows.*.pay"]) == ["rows.*.pay"]
    assert missing_fields({"pay": 0, "flag": False}, ["pay", "flag"]) == []
    assert missing_fields({"rows": []}, ["rows.*.pay"]) == ["rows.*.pay"]


def test_named_tool_permissions_checked_before_argument_generation():
    tools = registry()
    restricted = ExampleTool()
    restricted.permission_scope = ["private:read"]
    tools.register(restricted)
    with pytest.raises(WorkflowError) as failure:
        resolve_named_tool(tools, "first", permission_scopes=())
    assert failure.value.code == "tool_permission_denied"


def test_compiler_accepts_tool_names_adjacent_to_chinese_text():
    definition = SkillDefinition(
        name="skill", version="1", description="调用first，必须返回value。", definition_hash="hash"
    )
    workflow = compile_workflow(
        definition=definition,
        parent_task_id="parent",
        llm_client=CompilerLlm([step(source=definition.description)]),
        registry=registry(),
        permission_scopes=(),
    )
    assert workflow.steps[0].tool_name == "mcp.local.first"


def test_compiler_rejects_omitted_named_tool_invocation():
    definition = SkillDefinition(
        name="skill", version="1", description="调用工具 `first` 获取数据。", definition_hash="hash"
    )
    with pytest.raises(WorkflowError):
        compile_workflow(
            definition=definition,
            parent_task_id="parent",
            llm_client=CompilerLlm(
                [step(tool_name=None, required_fields=[], source=definition.description)]
            ),
            registry=registry(),
            permission_scopes=(),
        )


def test_compiler_rejects_invented_output_requirement():
    definition = SkillDefinition(
        name="skill",
        version="1",
        description="调用 first，必须返回 value。",
        definition_hash="hash",
    )
    llm = CompilerLlm([step()], output_requirement="表格")
    with pytest.raises(WorkflowError, match="最终输出要求"):
        compile_workflow(
            definition=definition,
            parent_task_id="parent",
            llm_client=llm,
            registry=registry(),
            permission_scopes=(),
        )


def test_compiler_rejects_omitted_numbered_analysis_step():
    summary = "第 1 步：调用 first，必须返回 value。\n第 2 步：分析结果并列出限制。\n用表格输出。"
    definition = SkillDefinition(
        name="skill", version="1", description=summary, definition_hash="hash"
    )
    only_first = step(source="第 1 步：调用 first，必须返回 value。")
    with pytest.raises(WorkflowError, match="第 2 步"):
        compile_workflow(
            definition=definition,
            parent_task_id="parent",
            llm_client=CompilerLlm([only_first]),
            registry=registry(),
            permission_scopes=(),
        )
