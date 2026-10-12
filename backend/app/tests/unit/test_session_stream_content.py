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


def test_research_process_collector_keeps_public_events_and_merges_activity_state():
    events = []
    sessions._collect_research_process_event(events, {
        "type": "progress",
        "kind": "status",
        "id": "planner",
        "message": "正在制定研究计划。",
        "status": "started",
    })
    sessions._collect_research_process_event(events, {
        "type": "progress",
        "kind": "summary",
        "id": "plan",
        "label": "研究计划",
        "message": "先核对公告，再比较行业数据。",
        "status": "completed",
    })
    sessions._collect_research_process_event(events, {
        "type": "progress",
        "kind": "activity",
        "id": "tool:news",
        "label": "获取证据",
        "message": "正在联网检索：近期公告",
        "status": "started",
    })
    sessions._collect_research_process_event(events, {
        "type": "progress",
        "kind": "activity",
        "id": "tool:news",
        "label": "获取证据",
        "message": "已完成联网检索：近期公告",
        "status": "completed",
    })

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

    sessions._collect_research_process_event(events, {
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
    })

    assert events[0]["activity_type"] == "market_data"
    assert events[0]["group_id"] == "market_data:1"
    assert events[0]["task_id"] == "daily"
    assert events[0]["task_label"] == "获取当日行情"


def test_assistant_metadata_includes_bounded_research_process():
    events = [{
        "id": "plan",
        "kind": "summary",
        "label": "研究计划",
        "message": "核对公开证据。",
        "status": "completed",
        "sequence": 0,
    }]

    metadata = sessions._assistant_response_metadata({}, research_process=events)

    assert metadata["research_process"] == events


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

    sessions._collect_research_process_event(events, {
        "type": "progress",
        "kind": "activity",
        "id": "activity:31",
        "label": "获取证据",
        "message": "已取得研究证据",
        "status": "completed",
    })

    assert len(events) == 32
    assert events[-1]["message"] == "已取得研究证据"
    assert events[-1]["status"] == "completed"


def test_protocol_stream_closes_upstream_when_consumer_disconnects():
    frames = _ClosableFrames(
        [sessions._sse("thinking_delta", {"content": "正在分析。"})]
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
