"""业务语义表达覆盖常规、流式、修复与原生工具调用；内部协议不变。"""

import json
from types import SimpleNamespace

import pytest
from app.llm.factory import invoke_llm_completion
from app.llm.schemas import LlmCompletionResult
from app.schemas.tools import ToolSpec
from app.tools.native_calls import create_tool_call_message
from langchain_core.messages import AIMessage, HumanMessage


@pytest.mark.parametrize("mode", ["ordinary", "stream", "retry"])
def test_all_completion_paths_receive_business_language_rule_without_changing_input(mode):
    calls = []

    def complete(**kwargs):
        calls.append(kwargs)
        return LlmCompletionResult(content="" if mode == "retry" and len(calls) == 1 else "业务结论",
                                   model="test", provider="test")

    class Client:
        async def complete(self, **kwargs):
            return complete(**kwargs)

    client = Client()
    if mode == "stream":
        client.complete_streamed_sync = complete
    raw = json.dumps({"tool_name": "mcp.local.facts", "result": {"net_profit": 12}})
    result = invoke_llm_completion(client, system_prompt="依据结果解释盈利情况", user_prompt=raw,
                                   thinking=mode == "retry", retry_without_thinking_on_empty=True,
                                   on_delta=(lambda event: None) if mode == "stream" else None)
    assert result.content == "业务结论"
    assert len(calls) == (2 if mode == "retry" else 1)
    for call in calls:
        assert "【业务语义表达】" in call["system_prompt"]
        assert call["user_prompt"] == raw


def test_native_call_keeps_tool_identity_and_arguments_but_constrains_natural_language():
    captured = {}

    class Client:
        def bind_tools(self, schemas, *, tool_choice):
            captured["schema"] = schemas[0]

            def invoke(messages):
                captured["messages"] = messages
                return AIMessage(content="", tool_calls=[{
                    "id": "model", "name": schemas[0]["function"]["name"],
                    "args": {"report_period": "2026Q3"},
                }])

            return SimpleNamespace(invoke=invoke)

    spec = ToolSpec(name="mcp.local.facts", version="1", description="查询季度利润",
                    input_schema={"type": "object", "properties": {"report_period": {"type": "string"}}})
    result = create_tool_call_message(llm_client=Client(), messages=[HumanMessage(content="查询第三季度利润")],
                                      tool_specs=[spec])
    assert "【业务语义表达】" in captured["messages"][0].content
    assert result.selected_tool.name == spec.name
    assert result.message.tool_calls[0]["args"] == {"report_period": "2026Q3"}
    assert "report_period" in captured["schema"]["function"]["parameters"]["properties"]


def test_router_does_not_promote_history_assistant_paths_to_current_requirements(business_intent_catalog):
    from app.intent.classifiers import LlmIntentClassifier
    from app.tests.unit.test_request_flow import Answers, intent

    llm = Answers(intent(route="plan"))
    LlmIntentClassifier(llm_client=llm, catalog=business_intent_catalog).classify(
        message="获取标的清单", context={"conversation_history": [
            {"role": "ai", "content": "建议分别查询两个范围，再下载名单。"},
        ]},
    )
    assert "历史助手建议" in llm.calls[0]["system_prompt"]
    assert "不固化取证路径" in llm.calls[0]["system_prompt"]


@pytest.mark.parametrize("review", [False, True])
def test_planner_checks_whole_goal_coverage_before_inheriting_a_path(review):
    from app.graph.planning.contracts import PlanModel
    from app.graph.planning.planner import LlmTaskPlanner, PlannerCapabilities
    from app.tests.unit.test_request_flow import Answers

    llm = Answers({"action": "finish", "reason": "已覆盖目标"} if review else {
        "tasks": [{"id": "facts", "task_goal": "获取完整标的清单"}],
    })
    planner = LlmTaskPlanner(llm_client=llm)
    common = {"goal": "获取完整标的清单", "capabilities": PlannerCapabilities(), "task_results": ()}
    if review:
        planner.review(**common, user_message="获取标的清单", remaining_revisions=1,
                        current_plan=PlanModel(tasks=[{"id": "facts", "task_goal": "获取完整标的清单"}]))
    else:
        planner.plan(**common, intent_type="financial_research", domain="equity", subdomain="general",
                      remaining_iterations=2)
    assert "优先检查当前已披露能力" in llm.calls[0]["system_prompt"]
    assert "历史建议" in llm.calls[0]["system_prompt"]
