import pytest
from app.graph import public_events
from app.llm.schemas import LlmStreamDelta


def test_public_llm_handler_keeps_reasoning_and_control_json_private(monkeypatch):
    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)

    handler = public_events.build_public_llm_stream_handler(
        "planner_agent",
        {"request_id": "req-1", "planner_iteration": 2},
    )
    handler(LlmStreamDelta(kind="reasoning", delta="先拆分投研目标。"))
    handler(LlmStreamDelta(kind="content", delta='{"tasks": []}'))

    assert emitted == []


def test_public_llm_handler_can_stream_synthesizer_answer(monkeypatch):
    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)

    handler = public_events.build_public_llm_stream_handler(
        "synthesizer",
        {"request_id": "req-1"},
        stream_content=True,
    )
    handler(LlmStreamDelta(kind="content", delta="## 结论"))
    handler.flush()

    assert emitted == [
        {
            "type": "answer_delta",
            "content": "## 结论",
        }
    ]


def test_public_llm_handler_batches_tiny_answer_deltas(monkeypatch):
    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)
    handler = public_events.build_public_llm_stream_handler(
        "synthesizer", {}, stream_content=True
    )

    answer = "下周行情需要结合成交量和政策信息判断。" * 20
    for character in answer:
        handler(LlmStreamDelta(kind="content", delta=character))
    handler.flush()

    assert "".join(item["content"] for item in emitted) == answer
    assert len(emitted) < len(answer) // 10


def test_public_llm_handler_has_no_raw_reasoning_mode(monkeypatch):
    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)

    with pytest.raises(TypeError):
        public_events.build_public_llm_stream_handler(
            "planner_agent",
            {"request_id": "req-1"},
            stream_reasoning=True,
        )


def test_public_llm_handler_never_emits_model_reasoning(monkeypatch):
    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)

    handler = public_events.build_public_llm_stream_handler(
        "synthesizer",
        {"request_id": "req-1"},
    )
    raw = "检查 allowed_taxonomy、skill_name、tool_name 和 required_schema。"
    handler(LlmStreamDelta(kind="reasoning", delta=raw))

    assert emitted == []


def test_tool_progress_uses_public_activity_text_without_tool_name(monkeypatch):
    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)

    public_events.emit_public_progress(
        "tool_agent",
        {
            "active_task": {
                "id": "recent_news",
                "task_goal": "核验银之杰近期新闻与公告",
                "tool_name": "web_search",
            }
        },
        status="started",
    )

    assert emitted[0]["message"] == "正在联网检索：核验银之杰近期新闻与公告"
    assert emitted[0]["kind"] == "activity"
    assert "web_search" not in emitted[0]["message"]


def test_completed_web_search_progress_contains_real_result_metadata(monkeypatch):
    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)

    public_events.emit_public_progress(
        "tool_agent",
        {
            "planner_iteration": 1,
            "active_task": {
                "id": "market",
                "task_goal": "搜索市场",
                "tool_name": "web_search",
            },
            "task_results": [
                {
                    "task_id": "market",
                    "status": "success",
                    "content": (
                        '{"query":"搜索市场","results":['
                        '{"url":"https://a.example/x"},'
                        '{"url":"https://a.example/x#section"},'
                        '{"url":"javascript:alert(1)"}]}'
                    ),
                }
            ],
        },
        status="completed",
    )

    assert emitted[0]["activity_type"] == "web_search"
    assert emitted[0]["group_id"] == "web_search:1"
    assert emitted[0]["task_id"] == "market"
    assert emitted[0]["query"] == "搜索市场"
    assert emitted[0]["result_urls"] == ["https://a.example/x"]
    assert emitted[0]["result_count"] == 1


def test_market_data_tools_publish_semantic_group_metadata(monkeypatch):
    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)

    public_events.emit_public_progress(
        "tool_agent",
        {
            "planner_iteration": 1,
            "active_task": {
                "id": "daily_price",
                "task_goal": "获取银之杰当日行情",
                "tool_name": "mcp.tushare.stock.daily",
            },
        },
        status="started",
    )

    assert emitted[0]["activity_type"] == "market_data"
    assert emitted[0]["group_id"] == "market_data:1"
    assert emitted[0]["task_id"] == "daily_price"
    assert emitted[0]["task_label"] == "获取银之杰当日行情"


def test_generic_tools_publish_evidence_group_metadata(monkeypatch):
    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)

    public_events.emit_public_progress(
        "tool_agent",
        {
            "planner_iteration": 2,
            "active_task": {
                "id": "announcement",
                "task_goal": "核验近期公告",
                "tool_name": "document_lookup",
            },
        },
        status="completed",
    )

    assert emitted[0]["activity_type"] == "evidence"
    assert emitted[0]["group_id"] == "evidence:2"
    assert emitted[0]["task_label"] == "核验近期公告"


def test_completed_progress_keeps_task_identity_with_partial_node_update(monkeypatch):
    from app.graph.runtime import _with_harness

    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)

    class Runner:
        def run(self, _name, node, state, **_kwargs):
            return node(state)

    task = {"id": "news", "task_goal": "核验银行板块新闻", "tool_name": "web_search"}
    update = {"task_results": [{"task_id": "news", "status": "success"}]}
    node = _with_harness("tool_agent", lambda _state: update, Runner())
    assert node({"errors": [], "active_task": task}) is update
    assert [event["id"] for event in emitted] == ["tool_agent:news", "tool_agent:news"]
    assert emitted[-1]["message"] == "已完成联网检索：核验银行板块新闻"
    assert emitted[-1]["kind"] == "activity"


def test_completed_planning_progress_keeps_iteration_identity(monkeypatch):
    from app.graph.runtime import _with_harness

    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)

    class Runner:
        def run(self, _name, node, state, **_kwargs):
            return node(state)

    def plan(state):
        state["planner_iteration"] += 1
        state["plan"] = {"tasks": []}
        return state

    _with_harness("planner_agent", plan, Runner())({"errors": [], "planner_iteration": 0})
    assert [event["id"] for event in emitted] == ["planner_agent:0", "planner_agent:0"]


def test_planner_completion_reuses_task_goals_without_summary(monkeypatch):
    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)

    public_events.emit_public_progress(
        "planner_agent",
        {
            "planner_iteration": 0,
            "plan": {
                "public_summary": "先核对公司公告与行业数据，再比较估值和风险。",
                "tasks": [{"id": "news", "task_goal": "核验近期公告"},
                          {"id": "valuation", "task_goal": "核对估值区间"}],
            },
        },
        status="completed",
    )

    assert emitted == [{
        "type": "progress",
        "kind": "plan",
        "id": "planner_agent:0",
        "stage": "planner_agent",
        "label": "研究计划",
        "status": "completed",
        "message": "1. 核验近期公告\n2. 核对估值区间",
    }]


@pytest.mark.parametrize("node", ["analysis_agent", "skill_agent"])
def test_completed_task_displays_its_goal_not_result_findings(monkeypatch, node):
    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)

    public_events.emit_public_progress(
        node,
        {
            "active_task": {"id": "compare", "task_goal": "比较估值"},
            "task_results": [{
                "task_id": "compare",
                "status": "success",
                "content": "现有证据显示估值分化明显。需要结合盈利增速判断。",
            }],
        },
        status="completed",
    )

    assert emitted[0]["kind"] == "activity"
    assert "比较估值" in emitted[0]["message"]
    assert "估值分化" not in emitted[0]["message"]


def test_unsafe_summary_falls_back_to_public_status(monkeypatch):
    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)

    public_events.emit_public_progress(
        "analysis_agent",
        {
            "active_task": {"id": "compare", "task_goal": "调用 web_search 后输出 required_schema"},
            "task_results": [{
                "task_id": "compare",
                "status": "success",
                "content": '{"required_schema": {"tool_name": "web_search"}}',
            }],
        },
        status="completed",
    )

    assert emitted[0]["kind"] == "activity"
    assert "web_search" not in emitted[0]["message"]


def test_result_text_is_never_used_as_a_public_summary(monkeypatch):
    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)

    public_events.emit_public_progress(
        "analysis_agent",
        {
            "active_task": {"id": "trend", "task_goal": "分析盈利趋势"},
            "task_results": [{
                "task_id": "trend",
                "status": "success",
                "content": "# 盈利趋势\n\n## 核心发现\n\n" + "利润增速高于收入增速。" * 30,
            }],
        },
        status="completed",
    )

    assert not emitted[0]["message"].startswith("#")
    assert len(emitted[0]["message"]) <= 240
    assert "利润增速" not in emitted[0]["message"]


def test_intent_completion_publishes_the_existing_goal(monkeypatch):
    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)
    public_events.emit_public_progress("intent_router", {"intent": {"goal": "分析当日市场表现"}}, status="completed")
    assert emitted[0]["kind"] == "goal"
    assert emitted[0]["label"] == "研究目标"
    assert emitted[0]["message"] == "分析当日市场表现"


def test_goal_and_each_plan_item_keep_full_text_while_activity_labels_stay_short(monkeypatch):
    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)
    goal = "核实不同期间的原始数据并区分推演输入与核对值。" * 25 + "完整目标结尾。"
    tasks = [{"id": f"facts_{index}", "task_goal": "取得必要披露数据。" * 25 + f"任务{index}结尾。"}
             for index in range(1, 3)]
    public_events.emit_public_progress("intent_router", {"intent": {"goal": goal}}, status="completed")
    public_events.emit_public_progress("planner_agent", {"plan": {"tasks": tasks}}, status="completed")
    public_events.emit_public_progress("tool_agent", {"active_task": tasks[0]}, status="started")

    assert emitted[0]["message"] == goal
    assert emitted[1]["message"] == "\n".join(
        f"{index}. {task['task_goal']}" for index, task in enumerate(tasks, 1)
    )
    assert len(emitted[2]["message"]) <= 240


def test_failed_analysis_is_not_displayed_as_completed(monkeypatch):
    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)
    public_events.emit_public_progress("analysis_agent", {
        "active_task": {"id": "compare", "task_goal": "比较估值"},
        "task_results": [{"task_id": "compare", "status": "terminal_failure", "content": "内部错误"}],
    }, status="completed")
    assert emitted[0]["status"] == "failed"
    assert emitted[0]["message"] == "分析未完成：比较估值"


def test_search_answer_uses_actual_tool_and_unwraps_recovery(monkeypatch):
    import json

    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)
    for wrapped in (False, True):
        payload = {"answer": "  搜索确认的摘要。  ", "results": [{"url": "https://a.example/x"}]}
        public_events.emit_public_progress("tool_agent", {
            "active_task": {"id": "search", "task_goal": "核验资料"},
            "task_results": [{"task_id": "search", "status": "success", "tool_name": "web_search",
                              "content": json.dumps({"search_result": payload} if wrapped else payload)}],
        }, status="completed")
        assert emitted[-1]["activity_type"] == "web_search"
        assert emitted[-1]["answer"] == "搜索确认的摘要。"
        assert emitted[-1]["result_count"] == 1


def test_search_answer_is_only_published_for_success_and_is_bounded(monkeypatch):
    import json

    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)
    for status, answer in (("success", " "), ("success", None), ("success", 17),
                           ("business_failure", "不能展示"), ("success", "字" * 5000)):
        state = {"active_task": {"id": "search", "tool_name": "web_search"},
                 "task_results": [{"task_id": "search", "status": status,
                                   "content": json.dumps({"answer": answer})}]}
        public_events.emit_public_progress("tool_agent", state, status="completed")
        if status == "success" and isinstance(answer, str) and answer.strip():
            assert emitted[-1]["answer"] == "字" * 4000
        else:
            assert "answer" not in emitted[-1]
    public_events.emit_public_progress("tool_agent", state, status="started")
    assert "answer" not in emitted[-1]


def test_search_progress_redacts_credentials_from_answer(monkeypatch):
    import json

    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)
    public_events.emit_public_progress("tool_agent", {
        "active_task": {"id": "search", "task_goal": "公开资料"},
        "task_results": [{"task_id": "search", "status": "success", "tool_name": "web_search",
                          "content": json.dumps({"answer": "摘要 api_key=synthetic-private-key"})}],
    }, status="completed")
    assert "synthetic-private-key" not in emitted[0]["answer"]
    assert "摘要" in emitted[0]["answer"]
