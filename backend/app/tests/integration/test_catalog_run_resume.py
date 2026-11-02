"""真实 checkpoint 恢复必须绑定同一 run 的目录，而非采用刷新后的目录。"""

import pytest
from app.graph import runtime
from app.graph.nodes.approval_gate import approval_gate_node
from app.graph.nodes.user_input_gate import user_input_gate_node
from app.graph.state import AgentState, build_checkpoint_thread_id, create_initial_state
from app.schemas.approvals import PendingToolApproval
from app.schemas.tools import ToolCallRecord, ToolCallRequest
from app.schemas.user_input import UserInputOption, UserInputRequest, UserInputSource
from app.tests.unit.test_catalog_refresh import ReadTool, registry_for
from app.tests.unit.test_request_flow import Answers, intent
from app.tools.catalog_runtime import ToolCatalogRuntime
from app.tools.fingerprints import tool_call_fingerprint
from app.tools.registry import ToolRegistry
from app.tools.search import ToolSearchIndex, ToolSearchService
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, StateGraph


@pytest.mark.parametrize("retain_snapshot", [True, False])
@pytest.mark.parametrize("kind", ["user_input", "approval"])
def test_input_resume_keeps_run_snapshot_or_reports_expiration_to_model(
    monkeypatch, business_intent_catalog, retain_snapshot, kind,
):
    current = [registry_for(ReadTool([]))]
    catalog = ToolCatalogRuntime(registry_factory=lambda: current[0], search_service_factory=lambda registry:
        ToolSearchService(registry=registry, index=ToolSearchIndex.build(
            specs=registry.specs(), catalog_digest=registry.catalog_digest), candidate_k=20))
    factory = lambda: catalog.snapshot()
    monkeypatch.setattr(runtime, "get_default_tool_registry", factory)
    monkeypatch.setattr(runtime, "_DEFAULT_TOOL_REGISTRY_FACTORY", factory)
    monkeypatch.setattr(runtime, "get_default_tool_catalog_runtime", lambda: catalog)
    state = create_initial_state(
        session_id="s", tenant_id="t", user_id="u", user_message="读取事实",
    )
    state["intent"] = intent(goal="读取事实")
    state["request_flow_version"] = "context-routing-v1"
    state["plan"] = {"tasks": [{"id": "facts", "task_goal": "读取事实", "execution_type": "tool"}]}
    state["planner_iteration"] = 1
    state["pending_user_input"] = UserInputRequest(
        interaction_id="choice", input_type="selection", question="选择查询范围",
        options=(UserInputOption(value="a", label="范围甲"), UserInputOption(value="b", label="范围乙")),
        source=UserInputSource(node="tool_agent", kind="choice", task_id="facts"),
    )
    key = build_checkpoint_thread_id(
        tenant_id="t", session_key=state["session_key"], request_id=state["request_id"],
    )
    original, _ = catalog.snapshot_pair_for_run(run_key=key)
    state["tool_catalog_digest"] = original.catalog_digest
    gate_name, gate = "user_input_gate", user_input_gate_node
    if kind == "approval":
        state["pending_user_input"] = None
        request = ToolCallRequest(call_id="staged", tool_name="read_facts", tool_version="1",
                                  arguments={}, purpose="读取事实", approval_required=True)
        state["tool_calls"] = [ToolCallRecord(call_id="staged", request=request)]
        state["pending_tool_approval"] = PendingToolApproval(
            task_id="facts", call_id="staged", step_index=0, request_fingerprint=tool_call_fingerprint(request),
            tool_name=request.tool_name, tool_version=request.tool_version, risk_level="high", purpose=request.purpose,
        )
        gate_name, gate = "approval_gate", approval_gate_node
    saver = InMemorySaver()
    graph = StateGraph(AgentState)
    graph.add_node(gate_name, gate)
    graph.set_entry_point(gate_name)
    graph.add_edge(gate_name, END)
    paused = graph.compile(checkpointer=saver).invoke(state, {"configurable": {"thread_id": key}})
    interrupted = paused["__interrupt__"][0]
    current[0] = ToolRegistry().snapshot()
    catalog.request_refresh()
    catalog._refresh_thread.join(timeout=5)
    if not retain_snapshot:
        catalog.release_run_snapshot(key)
    seen = []

    def worker(value):
        seen.append(value["tool_catalog_digest"])
        return {"task_results": [{"task_id": "facts", "status": "success", "content": "已有事实"}]}

    llm = Answers({"action": "answer", "answer": "根据当前资料说明查询结果。"})
    common = {
        "business_intent_catalog": business_intent_catalog, "checkpoint_thread_id": key,
        "interrupt_id": interrupted.id, "checkpointer": saver, "llm_client": llm,
        "tool_agent_node": worker, "allow_test_tool_agent_override": True,
    }
    if kind == "user_input":
        if not retain_snapshot:
            with pytest.raises(runtime.AgentGraphExecutionError) as rejected:
                runtime.resume_user_input_graph(**common, interaction_id="choice", input_type="selection", value="INVALID")
            assert rejected.value.code == "user_input_selection_invalid"
            assert catalog.get_run_snapshot(key) is None
        result = runtime.resume_user_input_graph(**common, interaction_id="choice", input_type="selection", value="a")
    else:
        snapshot = saver.get_tuple({"configurable": {"thread_id": key}})
        payload = interrupted.value
        if not retain_snapshot:
            from app.graph.nodes.approval_gate import ApprovalResumeError

            with pytest.raises(ApprovalResumeError):
                runtime.resume_question_answer_graph(**common,
                    checkpoint_id=snapshot.config["configurable"]["checkpoint_id"], action_digest=payload["action_digest"],
                    approval_decision={"approval_id": payload["approval_id"], "action_digest": payload["action_digest"],
                                       "decision": "INVALID"})
            assert catalog.get_run_snapshot(key) is None
        result = runtime.resume_question_answer_graph(**common,
            checkpoint_id=snapshot.config["configurable"]["checkpoint_id"], action_digest=payload["action_digest"],
            approval_decision={"approval_id": payload["approval_id"], "action_digest": payload["action_digest"], "decision": "approved"})
    assert result["final_response"] and "__interrupt__" not in result
    if retain_snapshot:
        assert seen == [original.catalog_digest]
    else:
        assert seen == []
        assert result["task_results"][0]["error_code"] == "tool_catalog_changed"
        assert "tool_catalog_changed" in llm.calls[0]["user_prompt"]
    assert catalog.get_run_snapshot(key) is None
    catalog.close()
