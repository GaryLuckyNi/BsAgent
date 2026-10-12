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


def test_public_llm_handler_streams_raw_reasoning_tokens_immediately(monkeypatch):
    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)

    handler = public_events.build_public_llm_stream_handler(
        "planner_agent",
        {"request_id": "req-1"},
        stream_reasoning=True,
    )
    handler(LlmStreamDelta(kind="reasoning", delta="第一"))
    handler(LlmStreamDelta(kind="reasoning", delta="段"))

    assert [event["content"] for event in emitted] == ["第一", "段"]
    assert {event["id"] for event in emitted} == {"planner_agent:reasoning"}
    assert {event["label"] for event in emitted} == {"制定计划"}


def test_reasoning_stream_preserves_model_text_without_filtering_or_rewriting(monkeypatch):
    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)

    handler = public_events.build_public_llm_stream_handler(
        "synthesizer",
        {"request_id": "req-1"},
        stream_reasoning=True,
    )
    raw = "The user wants a market judgment.\n用户要我先核对 required_schema。"
    handler(LlmStreamDelta(kind="reasoning", delta=raw))

    assert emitted[0]["content"] == raw


def test_empty_reasoning_delta_is_not_emitted(monkeypatch):
    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)

    handler = public_events.build_public_llm_stream_handler(
        "synthesizer",
        {"request_id": "req-1"},
        stream_reasoning=True,
    )
    handler(LlmStreamDelta(kind="reasoning", delta=""))

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


def test_planner_completion_publishes_structured_plan_summary(monkeypatch):
    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)

    public_events.emit_public_progress(
        "planner_agent",
        {
            "planner_iteration": 0,
            "plan": {
                "public_summary": "先核对公司公告与行业数据，再比较估值和风险。",
                "tasks": [{"id": "news"}, {"id": "valuation"}],
            },
        },
        status="completed",
    )

    assert emitted == [{
        "type": "progress",
        "kind": "summary",
        "id": "planner_agent:0",
        "stage": "planner_agent",
        "label": "研究计划",
        "status": "completed",
        "message": "先核对公司公告与行业数据，再比较估值和风险。",
    }]


def test_analysis_completion_uses_result_summary_without_internal_control_text(monkeypatch):
    emitted = []
    monkeypatch.setattr(public_events, "get_stream_writer", lambda: emitted.append)

    public_events.emit_public_progress(
        "analysis_agent",
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

    assert emitted[0]["kind"] == "summary"
    assert emitted[0]["message"] == "现有证据显示估值分化明显。需要结合盈利增速判断。"


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

    assert emitted[0]["kind"] == "status"
    assert "web_search" not in emitted[0]["message"]


def test_public_summary_removes_markdown_headings_and_is_bounded(monkeypatch):
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
