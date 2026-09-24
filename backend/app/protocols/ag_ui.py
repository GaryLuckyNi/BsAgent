from collections.abc import Callable, Iterable, Iterator
import json

from ag_ui.core import (
    ActivitySnapshotEvent,
    BaseEvent,
    Interrupt,
    ReasoningEndEvent,
    ReasoningMessageContentEvent,
    ReasoningMessageEndEvent,
    ReasoningMessageStartEvent,
    ReasoningStartEvent,
    RunErrorEvent,
    RunFinishedEvent,
    RunFinishedInterruptOutcome,
    RunFinishedSuccessOutcome,
    RunStartedEvent,
    StepFinishedEvent,
    StepStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
)


InterruptProvider = Callable[[list[str]], list[Interrupt]]


def adapt_approval_resume_to_ag_ui(
    result: dict,
    *,
    thread_id: str,
    run_id: str,
    interrupts: list[Interrupt] | None = None,
) -> Iterator[BaseEvent]:
    """Project the existing approval/checkpoint result onto AG-UI run events."""
    yield RunStartedEvent(thread_id=thread_id, run_id=run_id)
    if result.get("run_status") == "interrupted":
        yield RunFinishedEvent(
            thread_id=thread_id,
            run_id=run_id,
            result={"run_status": "interrupted"},
            outcome=RunFinishedInterruptOutcome(interrupts=interrupts or []),
        )
        return

    response = result.get("final_response")
    content = response.get("content", "") if isinstance(response, dict) else ""
    message_id = str(result.get("message_id") or f"assistant_{run_id}")
    if content:
        yield TextMessageStartEvent(message_id=message_id, role="assistant")
        yield TextMessageContentEvent(message_id=message_id, delta=str(content))
        yield TextMessageEndEvent(message_id=message_id)
    yield RunFinishedEvent(
        thread_id=thread_id,
        run_id=run_id,
        result={
            "run_status": result.get("run_status"),
            "trace_id": response.get("trace_id") if isinstance(response, dict) else None,
        },
        outcome=RunFinishedSuccessOutcome(),
    )


def adapt_session_sse_to_ag_ui(
    frames: Iterable[str],
    *,
    thread_id: str,
    run_id: str,
    interrupt_provider: InterruptProvider | None = None,
) -> Iterator[BaseEvent]:
    yield RunStartedEvent(thread_id=thread_id, run_id=run_id)
    message_id = f"assistant_{run_id}"
    message_started = False
    reasoning_message_id = f"reasoning_{run_id}"
    reasoning_started = False
    open_steps: set[str] = set()
    terminal = False

    try:
        for frame in frames:
            event_name, data = _parse_sse_frame(frame)
            if event_name == "status":
                accepted_message_id = data.get("message_id")
                if isinstance(accepted_message_id, str) and accepted_message_id:
                    message_id = accepted_message_id
                continue
            if event_name == "progress":
                step_name = str(data.get("id") or data.get("stage") or "agent_progress")
                status = str(data.get("status") or "started")
                if status == "completed":
                    if step_name in open_steps:
                        yield StepFinishedEvent(step_name=step_name)
                        open_steps.remove(step_name)
                elif step_name not in open_steps:
                    yield StepStartedEvent(step_name=step_name)
                    open_steps.add(step_name)
                content = {
                    "label": str(data.get("label") or "分析"),
                    "message": str(data.get("message") or ""),
                    "status": status,
                }
                for key in (
                    "activity_type",
                    "group_id",
                    "task_id",
                    "query",
                    "result_count",
                    "result_urls",
                ):
                    if key in data:
                        content[key] = data[key]
                yield ActivitySnapshotEvent(
                    message_id=f"activity:{step_name}",
                    activity_type="PROGRESS",
                    content=content,
                )
                continue
            if event_name == "thinking_delta":
                delta = str(data.get("content") or "")
                if not delta:
                    continue
                if not reasoning_started:
                    yield ReasoningStartEvent(message_id=reasoning_message_id)
                    yield ReasoningMessageStartEvent(
                        message_id=reasoning_message_id,
                        role="reasoning",
                    )
                    reasoning_started = True
                yield ReasoningMessageContentEvent(
                    message_id=reasoning_message_id,
                    delta=delta,
                    metadata={
                        "stage": data.get("stage"),
                        "label": data.get("label"),
                    },
                )
                continue
            if event_name == "answer_delta":
                delta = str(data.get("content") or "")
                if not delta:
                    continue
                if reasoning_started:
                    yield from _end_reasoning(reasoning_message_id)
                    reasoning_started = False
                if not message_started:
                    yield TextMessageStartEvent(
                        message_id=message_id,
                        role="assistant",
                    )
                    message_started = True
                yield TextMessageContentEvent(message_id=message_id, delta=delta)
                continue
            if event_name == "approval_required":
                if reasoning_started:
                    yield from _end_reasoning(reasoning_message_id)
                    reasoning_started = False
                if message_started:
                    yield TextMessageEndEvent(message_id=message_id)
                    message_started = False
                approval_ids = _string_list(data.get("approval_ids"))
                interrupts = _load_interrupts(
                    approval_ids,
                    interrupt_provider=interrupt_provider,
                )
                yield RunFinishedEvent(
                    thread_id=thread_id,
                    run_id=run_id,
                    result={"trace_id": data.get("trace_id")},
                    outcome=RunFinishedInterruptOutcome(interrupts=interrupts),
                )
                terminal = True
                break
            if event_name == "final":
                if reasoning_started:
                    yield from _end_reasoning(reasoning_message_id)
                    reasoning_started = False
                content = str(data.get("content") or "")
                if content and not message_started:
                    yield TextMessageStartEvent(
                        message_id=message_id,
                        role="assistant",
                    )
                    yield TextMessageContentEvent(message_id=message_id, delta=content)
                    message_started = True
                if message_started:
                    yield TextMessageEndEvent(message_id=message_id)
                    message_started = False
                yield RunFinishedEvent(
                    thread_id=thread_id,
                    run_id=run_id,
                    result=data,
                    outcome=RunFinishedSuccessOutcome(),
                )
                terminal = True
                break
            if event_name == "error":
                if reasoning_started:
                    yield from _end_reasoning(reasoning_message_id)
                    reasoning_started = False
                yield RunErrorEvent(
                    code=str(data.get("code") or "agent_run_error"),
                    message=str(data.get("message") or "Agent 运行失败。"),
                )
                terminal = True
                break
    except Exception as exc:
        yield RunErrorEvent(
            code="ag_ui_adapter_error",
            message="AG-UI 协议适配失败。",
            metadata={"errorType": type(exc).__name__},
        )
        return
    finally:
        close = getattr(frames, "close", None)
        if callable(close):
            close()

    if not terminal:
        if reasoning_started:
            yield from _end_reasoning(reasoning_message_id)
        yield RunErrorEvent(
            code="ag_ui_stream_incomplete",
            message="Agent 事件流未正常结束。",
        )


def _end_reasoning(message_id: str) -> Iterator[BaseEvent]:
    yield ReasoningMessageEndEvent(message_id=message_id)
    yield ReasoningEndEvent(message_id=message_id)


def _parse_sse_frame(frame: str) -> tuple[str, dict]:
    event_name = "message"
    data: dict = {}
    for line in frame.splitlines():
        if line.startswith("event:"):
            event_name = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            decoded = json.loads(line.removeprefix("data:").strip())
            if isinstance(decoded, dict):
                data = decoded
    return event_name, data


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _load_interrupts(
    approval_ids: list[str],
    *,
    interrupt_provider: InterruptProvider | None,
) -> list[Interrupt]:
    if interrupt_provider is not None:
        interrupts = interrupt_provider(approval_ids)
        if interrupts:
            return interrupts
    return [
        Interrupt(
            id=approval_id,
            reason="approval_required",
            message="当前操作需要审批后才能继续。",
            metadata={"approvalId": approval_id},
        )
        for approval_id in approval_ids
    ]
