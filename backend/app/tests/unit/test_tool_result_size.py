import json

from app.graph.nodes.tool_agent import _task_result, _tool_message_content
from app.graph.planning.contracts import SubTaskModel, TaskResultModel
from app.schemas.tools import ToolCallResult


def test_large_web_search_result_becomes_bounded_task_evidence():
    task = SubTaskModel(id="macro_events", task_goal="查询下周宏观事件")
    result = ToolCallResult(
        tool_call_id="tool_search",
        tool_name="web_search",
        status="success",
        trace_id="trace_1",
        result={
            "query": "下周宏观事件",
            "answer": "政策与经济数据需要核实。" * 450,
            "results": [
                {
                    "title": f"官方来源 {index}",
                    "url": f"https://example.org/source/{index}",
                    "snippet": "公告摘要。" * 600,
                    "published_at": "2026-09-19",
                }
                for index in range(5)
            ],
        },
    )
    content = _tool_message_content(result)
    assert len(content) > 8_000

    task_result = _task_result(task, result, content)

    assert isinstance(task_result, TaskResultModel)
    assert len(task_result.content) <= 8_000
    summary = json.loads(task_result.content)
    assert summary["results"][0]["url"] == "https://example.org/source/0"
    assert summary["answer"].startswith("政策与经济数据需要核实。")


def test_large_generic_tool_result_is_bounded_without_node_failure():
    task = SubTaskModel(id="market_data", task_goal="读取市场数据")
    result = ToolCallResult(
        tool_call_id="tool_data",
        tool_name="market_data",
        status="success",
        trace_id="trace_1",
        result={"data": '"' * 10_000},
    )

    task_result = _task_result(task, result, _tool_message_content(result))

    assert len(task_result.content) <= 8_000
    assert json.loads(task_result.content)["truncated"] is True
