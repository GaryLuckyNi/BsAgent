"""只使用既有固定流程/JSON schema，不用关键词识别量化任务。"""

import pytest
from app.graph.nodes.skill_step_check import build_skill_step_check_node
from app.graph.nodes.tool_agent import _trusted_call_id
from app.graph.planning.contracts import SubTaskModel
from app.graph.planning.skill_workflow import dispatch_workflow
from app.graph.state import create_initial_state
from app.schemas.tools import ToolCallRecord, ToolCallRequest, ToolCallResult, ToolSpec
from app.tools.execution_gateway import validate_tool_arguments


class NoModel:
    async def complete(self, **kwargs):
        raise AssertionError("确定性数据缺口不应增加验收模型调用")


@pytest.mark.parametrize("original", ["missing", "failed", "uncertain"])
def test_search_article_cannot_replace_fixed_step_canonical_output_before_calculation(original):
    state = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="计算")
    state["plan"] = {"tasks": [{"id": "parent", "task_goal": "计算", "skill_name": "fixed"}]}
    state["skill_workflows"] = {
        "parent": {
            "parent_task_id": "parent",
            "skill_name": "fixed",
            "skill_version": "1",
            "definition_hash": "frozen",
            "summary": "先取得实际数据，再计算",
            "output_requirement": "",
            "steps": [
                {
                    "task_id": "series",
                    "instruction": "取数",
                    "source": "取数",
                    "tool_name": "mcp.x.series",
                    "tool_version": "1",
                    "required_fields": [],
                },
                {
                    "task_id": "calculate",
                    "instruction": "计算",
                    "source": "计算",
                    "tool_name": "mcp.x.calculate",
                    "tool_version": "1",
                },
            ],
            "checked_steps": [],
            "status": "running",
        }
    }
    task = SubTaskModel(id="series", task_goal="取数")
    state["active_task"] = task.model_dump()
    state["active_skill_step"] = {"parent_task_id": "parent", "index": 0}
    state["task_results"] = [
        {
            "task_id": "series",
            "status": "success",
            "content": "搜索相关文章；连续20日数据仍缺失",
            "tool_name": "web_search",
        }
    ]
    if original != "missing":
        call_id = _trusted_call_id(state, task, "mcp.x.series")
        state["tool_calls"] = [
            ToolCallRecord(
                call_id=call_id,
                request=ToolCallRequest(
                    call_id=call_id,
                    tool_name="mcp.x.series",
                    tool_version="1",
                    arguments={},
                    purpose="取数",
                ),
                result=ToolCallResult(
                    tool_call_id="result",
                    tool_name="mcp.x.series",
                    status="success" if original == "uncertain" else "denied",
                    execution_state="uncertain",
                    trace_id="t",
                ),
            )
        ]
    update = build_skill_step_check_node(llm_client=NoModel())(state)
    assert update["task_results"][0]["status"] == "terminal_failure"
    state["task_results"] = update["task_results"]
    state["skill_workflows"].update(update["skill_workflows"])
    assert all(send.node != "tool_agent" for send in dispatch_workflow(state))


def test_executable_sequence_schema_rejects_articles_and_incomplete_data():
    spec = ToolSpec(
        name="calculate",
        version="1",
        description="计算",
        input_schema={
            "type": "object",
            "required": ["series"],
            "properties": {
                "series": {
                    "type": "array",
                    "minItems": 20,
                    "items": {
                        "type": "object",
                        "required": ["date", "close"],
                        "properties": {"date": {"type": "string"}, "close": {"type": "number"}},
                    },
                }
            },
        },
    )
    for arguments in (
        {"series": "相关文章"},
        {"series": [{"date": "2020-01-01", "close": 1}]},
        {"answer": "二十日涨幅"},
    ):
        assert not validate_tool_arguments(tool_spec=spec, arguments=arguments).valid
