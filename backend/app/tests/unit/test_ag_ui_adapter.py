from ag_ui.core import EventType, Interrupt

from app.protocols.ag_ui import (
    adapt_approval_resume_to_ag_ui,
    adapt_session_sse_to_ag_ui,
)


def _sse(event: str, data: dict) -> str:
    import json

    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


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


def test_ag_ui_adapter_closes_upstream_when_consumer_disconnects():
    frames = _ClosableFrames([_sse("thinking_delta", {"content": "正在分析。"})])
    stream = adapt_session_sse_to_ag_ui(
        frames,
        thread_id="session-1",
        run_id="client-run-1",
    )

    next(stream)
    next(stream)
    stream.close()

    assert frames.closed is True


def test_ag_ui_adapter_emits_typed_run_step_and_text_lifecycle():
    frames = [
        _sse(
            "status",
            {
                "stage": "accepted",
                "message_id": "assistant-1",
                "run_id": "internal-run-1",
            },
        ),
        _sse(
            "progress",
            {
                "id": "planner:0",
                "stage": "planner_agent",
                "label": "制定计划",
                "status": "started",
                "message": "正在拆分投研子任务。",
            },
        ),
        _sse(
            "progress",
            {
                "id": "planner:0",
                "stage": "planner_agent",
                "label": "制定计划",
                "status": "completed",
                "message": "已形成 2 个可执行子任务。",
            },
        ),
        _sse("answer_delta", {"content": "第一段"}),
        _sse("answer_delta", {"content": "第二段"}),
        _sse(
            "final",
            {
                "content": "第一段第二段",
                "trace_id": "trace-1",
                "approval_status": "not_required",
            },
        ),
        _sse("done", {"ok": True}),
    ]

    events = list(
        adapt_session_sse_to_ag_ui(
            frames,
            thread_id="session-1",
            run_id="client-run-1",
        )
    )

    assert [event.type for event in events] == [
        EventType.RUN_STARTED,
        EventType.STEP_STARTED,
        EventType.ACTIVITY_SNAPSHOT,
        EventType.STEP_FINISHED,
        EventType.ACTIVITY_SNAPSHOT,
        EventType.TEXT_MESSAGE_START,
        EventType.TEXT_MESSAGE_CONTENT,
        EventType.TEXT_MESSAGE_CONTENT,
        EventType.TEXT_MESSAGE_END,
        EventType.RUN_FINISHED,
    ]
    assert events[0].thread_id == "session-1"
    assert events[0].run_id == "client-run-1"
    assert events[2].content == {
        "label": "制定计划",
        "message": "正在拆分投研子任务。",
        "status": "started",
    }
    assert events[3].step_name == "planner:0"
    assert events[4].content["status"] == "completed"
    assert events[6].message_id == "assistant-1"
    assert events[6].delta == "第一段"
    assert events[-1].outcome.type == "success"
    assert events[-1].result["trace_id"] == "trace-1"


def test_ag_ui_adapter_preserves_search_activity_metadata():
    events = list(
        adapt_session_sse_to_ag_ui(
            [
                _sse(
                    "progress",
                    {
                        "id": "tool_agent:market",
                        "stage": "tool_agent",
                        "label": "获取证据",
                        "status": "completed",
                        "message": "已完成联网检索：搜索市场",
                        "activity_type": "web_search",
                        "group_id": "web_search:1",
                        "task_id": "market",
                        "query": "搜索市场",
                        "result_count": 2,
                        "result_urls": [
                            "https://a.example/x",
                            "https://b.example/y",
                        ],
                    },
                ),
                _sse("final", {"content": "完成", "trace_id": "trace-1"}),
            ],
            thread_id="session-1",
            run_id="run-1",
        )
    )

    activity = next(event for event in events if event.type == EventType.ACTIVITY_SNAPSHOT)
    assert activity.content["activity_type"] == "web_search"
    assert activity.content["group_id"] == "web_search:1"
    assert activity.content["task_id"] == "market"
    assert activity.content["result_count"] == 2
    assert activity.content["result_urls"] == [
        "https://a.example/x",
        "https://b.example/y",
    ]


def test_ag_ui_adapter_emits_native_reasoning_lifecycle_before_answer():
    frames = [
        _sse(
            "status",
            {"stage": "accepted", "message_id": "assistant-1"},
        ),
        _sse(
            "thinking_delta",
            {
                "stage": "planner_agent",
                "label": "制定计划",
                "content": "先明确用户希望比较的标的。",
            },
        ),
        _sse(
            "thinking_delta",
            {
                "stage": "planner_agent",
                "label": "制定计划",
                "content": "然后根据依赖获取证据。",
            },
        ),
        _sse("answer_delta", {"content": "## 结论"}),
        _sse(
            "final",
            {
                "content": "## 结论",
                "trace_id": "trace-1",
                "approval_status": "not_required",
            },
        ),
    ]

    events = list(
        adapt_session_sse_to_ag_ui(
            frames,
            thread_id="session-1",
            run_id="client-run-1",
        )
    )

    assert [event.type for event in events] == [
        EventType.RUN_STARTED,
        EventType.REASONING_START,
        EventType.REASONING_MESSAGE_START,
        EventType.REASONING_MESSAGE_CONTENT,
        EventType.REASONING_MESSAGE_CONTENT,
        EventType.REASONING_MESSAGE_END,
        EventType.REASONING_END,
        EventType.TEXT_MESSAGE_START,
        EventType.TEXT_MESSAGE_CONTENT,
        EventType.TEXT_MESSAGE_END,
        EventType.RUN_FINISHED,
    ]
    assert events[3].delta == "先明确用户希望比较的标的。"
    assert events[4].delta == "然后根据依赖获取证据。"


def test_ag_ui_adapter_exposes_interrupt_as_run_outcome():
    frames = [
        _sse(
            "status",
            {"stage": "accepted", "message_id": "assistant-1"},
        ),
        _sse(
            "approval_required",
            {"approval_ids": ["approval-1"], "trace_id": "trace-1"},
        ),
        _sse("done", {"ok": True, "pending_approval": True}),
    ]
    requested_ids = []

    def load_interrupts(approval_ids: list[str]) -> list[Interrupt]:
        requested_ids.extend(approval_ids)
        return [
            Interrupt(
                id="interrupt-1",
                reason="approval_required",
                message="需要批准高风险工具调用。",
                metadata={"approvalId": "approval-1", "approvalType": "tool_call"},
            )
        ]

    events = list(
        adapt_session_sse_to_ag_ui(
            frames,
            thread_id="session-1",
            run_id="client-run-1",
            interrupt_provider=load_interrupts,
        )
    )

    assert requested_ids == ["approval-1"]
    assert [event.type for event in events] == [
        EventType.RUN_STARTED,
        EventType.RUN_FINISHED,
    ]
    assert events[-1].outcome.type == "interrupt"
    assert events[-1].outcome.interrupts[0].id == "interrupt-1"
    assert events[-1].outcome.interrupts[0].metadata["approvalId"] == "approval-1"


def test_ag_ui_adapter_maps_agent_error_to_terminal_run_error():
    frames = [
        _sse("status", {"stage": "accepted"}),
        _sse(
            "error",
            {"code": "tool_timeout", "message": "市场数据服务暂时不可用。"},
        ),
        _sse("done", {"ok": False}),
    ]

    events = list(
        adapt_session_sse_to_ag_ui(
            frames,
            thread_id="session-1",
            run_id="client-run-1",
        )
    )

    assert [event.type for event in events] == [
        EventType.RUN_STARTED,
        EventType.RUN_ERROR,
    ]
    assert events[-1].code == "tool_timeout"
    assert events[-1].message == "市场数据服务暂时不可用。"


def test_ag_ui_adapter_maps_completed_approval_resume_to_text_and_success():
    events = list(
        adapt_approval_resume_to_ag_ui(
            {
                "run_status": "completed",
                "message_id": "assistant-1",
                "final_response": {
                    "content": "审批通过，任务已完成。",
                    "trace_id": "trace-1",
                },
            },
            thread_id="session-1",
            run_id="resume-run-1",
        )
    )

    assert [event.type for event in events] == [
        EventType.RUN_STARTED,
        EventType.TEXT_MESSAGE_START,
        EventType.TEXT_MESSAGE_CONTENT,
        EventType.TEXT_MESSAGE_END,
        EventType.RUN_FINISHED,
    ]
    assert events[2].delta == "审批通过，任务已完成。"
    assert events[-1].outcome.type == "success"
    assert events[-1].result["run_status"] == "completed"


def test_ag_ui_adapter_maps_chained_approval_resume_to_interrupt_outcome():
    interrupts = [
        Interrupt(
            id="interrupt-2",
            reason="approval_required",
            message="下一步仍需审批。",
        )
    ]

    events = list(
        adapt_approval_resume_to_ag_ui(
            {"run_status": "interrupted", "final_response": None},
            thread_id="session-1",
            run_id="resume-run-1",
            interrupts=interrupts,
        )
    )

    assert [event.type for event in events] == [
        EventType.RUN_STARTED,
        EventType.RUN_FINISHED,
    ]
    assert events[-1].outcome.type == "interrupt"
    assert events[-1].outcome.interrupts[0].id == "interrupt-2"
