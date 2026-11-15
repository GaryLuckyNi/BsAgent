"""公开研究字段随生成更新，执行仍使用校验后的完整结构。"""

import json

import pytest
from app.llm.schemas import LlmCompletionResult, LlmStreamDelta


def test_goal_projection_does_not_expose_nested_fields_or_reasoning():
    from app.graph.planning.progress_stream import ProgressFieldStream

    shown = []
    stream = ProgressFieldStream(kind="goal", on_text=shown.append)
    stream(LlmStreamDelta(kind="reasoning", delta="内部推理"))
    stream(LlmStreamDelta(kind="content", delta='{"available_information":{"goal":"不能展示"},"goal":"核实'))
    assert shown == ["核实"]
    stream(LlmStreamDelta(kind="content", delta='财报并判断经营质量。","route":"plan"}'))
    stream.flush()
    assert shown[-1] == "核实财报并判断经营质量。"
    assert "不能展示" not in "".join(shown)
    assert "route" not in "".join(shown)


@pytest.mark.parametrize("feedback", [False, True])
def test_plan_projection_grows_before_entire_json_finishes(feedback):
    from app.graph.planning.progress_stream import ProgressFieldStream

    shown = []
    stream = ProgressFieldStream(kind="plan", on_text=shown.append, feedback=feedback)
    prefix = '{"action":"revise","plan":' if feedback else ''
    stream(LlmStreamDelta(kind="content", delta=prefix + '{"tasks":[{"task_goal":"核实公告'))
    assert shown == ["1. 核实公告"]
    stream(LlmStreamDelta(kind="content", delta='全文。","tool_search_query":"内部检索字段"},{"task_goal":"取得财报原始数据。"}]}'))
    stream.flush()
    assert shown[-1] == "1. 核实公告全文。\n2. 取得财报原始数据。"
    assert "内部检索字段" not in "".join(shown)


def test_escaped_goal_is_decoded_and_small_updates_are_batched():
    from app.graph.planning.progress_stream import ProgressFieldStream

    shown = []
    stream = ProgressFieldStream(kind="goal", on_text=shown.append, clock=lambda: 0)
    goal = '核实“原始披露”与"用户条件"的区别。' * 15
    raw = json.dumps({"goal": goal}, ensure_ascii=True)
    for char in raw:
        stream(LlmStreamDelta(kind="content", delta=char))
    stream.flush()
    assert shown[-1] == goal
    assert len(shown) < len(goal) // 4


@pytest.mark.parametrize("worker", ["router", "planner", "review"])
def test_nodes_publish_public_fields_only_after_model_returns(monkeypatch, business_intent_catalog, worker):
    from app.graph import public_events
    from app.graph.nodes.intent_router import build_intent_router_node
    from app.graph.nodes.planner_agent import build_planner_agent_node
    from app.graph.state import create_initial_state

    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)
    state = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="核实公告")
    state["intent"] = {"intent_type": "financial_research", "domain": "a_share_equity_research",
                       "subdomain": "company_snapshot", "goal": "核实公告"}
    if worker == "router":
        response = {**state["intent"], "confidence": 0.9, "route": "plan"}
        prefix = '{"goal":"正在明确公告核验范围'
    else:
        response = {"tasks": [{"id": "facts", "task_goal": "取得公告全文并核对关键数字。"}]}
        prefix = '{"tasks":[{"task_goal":"正在安排公告取证'
        if worker == "review":
            state["plan"] = {"tasks": [{"id": "old", "task_goal": "查询已有资料"}]}
            state["planner_iteration"] = 1
            state["task_results"] = [{"task_id": "old", "status": "success", "content": "公告线索"}]
            response = {"action": "revise", "reason": "需要公告全文", "plan": response}
            prefix = '{"action":"revise","plan":' + prefix

    class Client:
        def complete_streamed_sync(self, **kwargs):
            assert kwargs["thinking"] is False
            kwargs["on_delta"](LlmStreamDelta(kind="reasoning", delta="不得公开的思维链"))
            kwargs["on_delta"](LlmStreamDelta(kind="content", delta=prefix))
            assert emitted == [], "未完成统一检查前不展示模型生成的字段"
            assert "不得公开" not in json.dumps(emitted, ensure_ascii=False)
            return LlmCompletionResult(content=json.dumps(response), model="fake", provider="fake")

    if worker == "router":
        build_intent_router_node(catalog=business_intent_catalog, llm_client=Client())(state)
        assert state["intent"]["goal"] == response["goal"]
    else:
        build_planner_agent_node(llm_client=Client())(state)
        assert state["plan"]["tasks"][0]["task_goal"] == "取得公告全文并核对关键数字。"
    assert any(event["kind"] == ("goal" if worker == "router" else "plan") for event in emitted)
    assert "不得公开" not in json.dumps(emitted, ensure_ascii=False)
    assert "正在安排" not in json.dumps(emitted, ensure_ascii=False)


@pytest.mark.parametrize("worker", ["router", "planner", "review"])
def test_failed_generation_does_not_publish_unchecked_preview(monkeypatch, business_intent_catalog, worker):
    from app.graph import public_events
    from app.graph.nodes.intent_router import build_intent_router_node
    from app.graph.nodes.planner_agent import build_planner_agent_node
    from app.graph.state import create_initial_state

    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)
    state = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="核实公告")
    state["intent"] = {"intent_type": "financial_research", "domain": "a_share_equity_research",
                       "subdomain": "company_snapshot", "goal": "核实公告"}
    if worker == "review":
        state["plan"] = {"tasks": [{"id": "facts", "task_goal": "核实公告"}]}
        state["planner_iteration"] = 1

    class Client:
        def complete_streamed_sync(self, **kwargs):
            prefix = '{"goal":"未确认的草稿' if worker == "router" else '{"tasks":[{"task_goal":"未确认的草稿'
            if worker == "review":
                prefix = '{"action":"revise","plan":' + prefix
            kwargs["on_delta"](LlmStreamDelta(kind="content", delta=prefix))
            raise RuntimeError("生成中断")

    with pytest.raises(RuntimeError):
        if worker == "router":
            build_intent_router_node(catalog=business_intent_catalog, llm_client=Client())(state)
        else:
            build_planner_agent_node(llm_client=Client())(state)
    assert emitted == []
