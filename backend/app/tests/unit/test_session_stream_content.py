import asyncio

from app.api import sessions


class _ClosableFrames:
    def __init__(self, frames: list[str]) -> None:
        self._frames = iter(frames)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self) -> str:
        return next(self._frames)

    def close(self) -> None:
        self.closed = True


def test_protocol_stream_ignores_old_internal_model_timing_without_affecting_body():
    frames = [
        sessions._sse("answer_started", {"started_at": "2026-10-29T00:00:05+00:00"}),
        sessions._sse("answer_delta", {"content": "结论。"}),
        sessions._sse("final", {"content": "结论。"}),
    ]
    events = [sessions._parse_sse_frame(frame) for frame in sessions._to_agent_protocol_stream(frames)]
    assert [name for name, _ in events] == ["messages", "custom"]


def test_protocol_stream_preserves_source_metadata_before_answer():
    sources = [{"source_id": "source_7", "index": 7, "url": "https://example.com/report", "title": "报告"}]
    frames = [
        sessions._sse("answer_sources", {"sources": sources}),
        sessions._sse("answer_delta", {"content": "结论。"}),
    ]
    events = [sessions._parse_sse_frame(frame) for frame in sessions._to_agent_protocol_stream(frames)]
    assert events[0] == ("custom", {"type": "answer_sources", "sources": sources})
    assert events[1][0] == "messages"


def test_research_process_collector_keeps_public_events_and_merges_activity_state():
    events = []
    sessions._collect_research_process_event(
        events,
        {
            "type": "progress",
            "kind": "status",
            "id": "planner",
            "message": "正在制定研究计划。",
            "status": "started",
        },
    )
    sessions._collect_research_process_event(
        events,
        {
            "type": "progress",
            "kind": "summary",
            "id": "plan",
            "label": "研究计划",
            "message": "先核对公告，再比较行业数据。",
            "status": "completed",
        },
    )
    sessions._collect_research_process_event(
        events,
        {
            "type": "progress",
            "kind": "activity",
            "id": "tool:news",
            "label": "获取证据",
            "message": "正在联网检索：近期公告",
            "status": "started",
        },
    )
    sessions._collect_research_process_event(
        events,
        {
            "type": "progress",
            "kind": "activity",
            "id": "tool:news",
            "label": "获取证据",
            "message": "已完成联网检索：近期公告",
            "status": "completed",
        },
    )

    assert events == [
        {
            "id": "plan",
            "kind": "summary",
            "label": "研究计划",
            "message": "先核对公告，再比较行业数据。",
            "status": "completed",
            "sequence": 0,
        },
        {
            "id": "tool:news",
            "kind": "activity",
            "label": "获取证据",
            "message": "已完成联网检索：近期公告",
            "status": "completed",
            "sequence": 1,
        },
    ]


def test_research_process_collector_preserves_activity_group_metadata():
    events = []

    sessions._collect_research_process_event(
        events,
        {
            "type": "progress",
            "kind": "activity",
            "id": "tool:daily",
            "label": "获取证据",
            "message": "已取得市场数据：获取当日行情",
            "status": "completed",
            "activity_type": "market_data",
            "group_id": "market_data:1",
            "task_id": "daily",
            "task_label": "获取当日行情",
        },
    )

    assert events[0]["activity_type"] == "market_data"
    assert events[0]["group_id"] == "market_data:1"
    assert events[0]["task_id"] == "daily"
    assert events[0]["task_label"] == "获取当日行情"


def test_assistant_metadata_includes_bounded_research_process():
    events = [
        {
            "id": "plan",
            "kind": "summary",
            "label": "研究计划",
            "message": "核对公开证据。",
            "status": "completed",
            "sequence": 0,
        }
    ]

    metadata = sessions._assistant_response_metadata({}, research_process=events)

    assert metadata["research_process"] == events


def test_goal_and_complete_long_plan_are_persisted_without_model_summary():
    events = []
    goal = "完整研究目标及必要的推演和核验要求。" * 40 + "目标结尾。"
    plan = "\n".join(f"{index}. 子任务{index}：" + "必要取证条件" * 65 + "任务结尾。" for index in range(1, 13))
    for kind, message in [("goal", goal), ("plan", plan)]:
        sessions._collect_research_process_event(events, {
            "id": kind, "kind": kind, "label": "研究目标" if kind == "goal" else "研究计划",
            "message": message, "status": "completed",
        })
    assert [event["kind"] for event in events] == ["goal", "plan"]
    assert events[0]["message"] == goal
    assert events[1]["message"] == plan
    assert "12. 子任务12" in events[1]["message"]


def test_session_api_has_no_private_reasoning_persistence_path():
    assert not hasattr(sessions, "_collect_research_reasoning_delta")


def test_research_process_collector_updates_existing_activity_after_limit():
    events = [
        {
            "id": f"activity:{index}",
            "kind": "activity",
            "label": "获取证据",
            "message": "正在获取证据",
            "status": "started",
            "sequence": index,
        }
        for index in range(32)
    ]

    sessions._collect_research_process_event(
        events,
        {
            "type": "progress",
            "kind": "activity",
            "id": "activity:31",
            "label": "获取证据",
            "message": "已取得研究证据",
            "status": "completed",
        },
    )

    assert len(events) == 32
    assert events[-1]["message"] == "已取得研究证据"
    assert events[-1]["status"] == "completed"


def test_protocol_stream_closes_upstream_when_consumer_disconnects():
    frames = _ClosableFrames(
        [
            sessions._sse(
                "progress",
                {
                    "kind": "status",
                    "id": "analysis",
                    "message": "正在分析。",
                    "status": "started",
                },
            )
        ]
    )
    stream = sessions._to_agent_protocol_stream(frames)

    next(stream)
    stream.close()

    assert frames.closed is True


def test_async_stream_bridge_closes_sync_iterator_when_consumer_disconnects():
    frames = _ClosableFrames(["first", "second"])

    async def disconnect() -> None:
        stream = sessions._closeable_async_stream(frames)
        assert await anext(stream) == "first"
        await stream.aclose()

    asyncio.run(disconnect())

    assert frames.closed is True


def test_search_answer_is_persisted_only_on_successful_search_activity():
    for activity, status, answer, expected in (
        ("web_search", "completed", "  搜索摘要  ", "搜索摘要"),
        ("web_search", "completed", "字" * 5000, "字" * 4000),
        ("web_search", "completed", " ", None),
        ("web_search", "failed", "错误中的文本", None),
        ("market_data", "completed", "普通结果", None),
    ):
        events = []
        sessions._collect_research_process_event(events, {
            "type": "progress", "kind": "activity", "id": "tool:search", "message": "获取证据",
            "activity_type": activity, "status": status, "answer": answer,
        })
        metadata = sessions._assistant_response_metadata({}, research_process=events)
        event = metadata["research_process"][0]
        assert event.get("answer") == expected
        if expected is None:
            assert "answer" not in event
