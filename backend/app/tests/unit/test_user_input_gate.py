import pytest
from app.graph.nodes import user_input_gate
from app.graph.state import create_initial_state
from app.schemas.user_input import (
    UserInputOption,
    UserInputRequest,
    UserInputSource,
)


def _request() -> UserInputRequest:
    return UserInputRequest(
        interaction_id="interaction_1",
        input_type="selection",
        question="请选择一项",
        options=[
            UserInputOption(value="first", label="第一项"),
            UserInputOption(value="second", label="第二项"),
        ],
        source=UserInputSource(node="producer", kind="example", task_id="task_1"),
    )


def _state():
    state = create_initial_state(
        session_id="session_1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="需要澄清",
    )
    state["pending_user_input"] = _request()
    return state


def test_gate_without_pending_request_is_a_noop():
    state = _state()
    state["pending_user_input"] = None

    assert user_input_gate.user_input_gate_node(state) == {}


def test_gate_projects_public_payload_and_copies_trusted_source(monkeypatch):
    captured = {}

    def resume(payload):
        captured.update(payload)
        return {
            "interaction_id": "interaction_1",
            "input_type": "selection",
            "value": "second",
        }

    monkeypatch.setattr(user_input_gate, "interrupt", resume)

    update = user_input_gate.user_input_gate_node(_state())

    assert captured == {
        "interrupt_type": "user_input",
        "interaction_id": "interaction_1",
        "input_type": "selection",
        "question": "请选择一项",
        "options": [
            {"value": "first", "label": "第一项", "description": None},
            {"value": "second", "label": "第二项", "description": None},
        ],
    }
    assert update["pending_user_input"] is None
    assert update["user_input_response"].value == "second"
    assert update["user_input_response"].source.kind == "example"


@pytest.mark.parametrize(
    ("resume", "code"),
    [
        (
            {
                "interaction_id": "wrong",
                "input_type": "selection",
                "value": "first",
            },
            "user_input_resume_mismatch",
        ),
        (
            {
                "interaction_id": "interaction_1",
                "input_type": "selection",
                "value": "outside",
            },
            "user_input_selection_invalid",
        ),
    ],
)
def test_gate_rejects_mismatched_or_outside_resume_value(monkeypatch, resume, code):
    monkeypatch.setattr(user_input_gate, "interrupt", lambda _payload: resume)

    with pytest.raises(user_input_gate.UserInputResumeError) as exc_info:
        user_input_gate.user_input_gate_node(_state())

    assert exc_info.value.code == code


@pytest.mark.parametrize("metadata", [{}, {"presentation_version": "permission-recovery-batch-v1"},
    {"presentation_version": "old", "batch_digest": "a" * 64},
    {"presentation_version": "permission-recovery-batch-v1", "batch_digest": "b" * 64}])
def test_batch_gate_requires_matching_presentation_before_accepting(monkeypatch, metadata):
    from app.tests.unit.test_user_input_schemas import batch_request

    state = _state()
    state["pending_user_input"] = batch_request()
    accepted = []
    monkeypatch.setattr(user_input_gate, "interrupt", lambda payload: {
        "interaction_id": "interaction_batch", "input_type": "selection", "value": "web_search",
        **metadata,
    })
    with pytest.raises(user_input_gate.UserInputResumeError) as exc_info:
        user_input_gate.user_input_gate_node(state, accept_recovery=lambda *args: accepted.append(args))
    assert exc_info.value.code == "permission_recovery_presentation_mismatch"
    assert accepted == []


def test_batch_gate_projects_all_items_and_records_decision_before_response(monkeypatch):
    from app.tests.unit.test_user_input_schemas import batch_request

    state = _state()
    request = batch_request()
    state["pending_user_input"] = request
    captured, accepted = {}, []

    def resume(payload):
        captured.update(payload)
        return {"interaction_id": request.interaction_id, "input_type": "selection",
                "value": "web_search", "presentation_version": request.recovery_batch.version,
                "batch_digest": request.recovery_batch.digest}

    monkeypatch.setattr(user_input_gate, "interrupt", resume)
    update = user_input_gate.user_input_gate_node(state, accept_recovery=lambda *args: accepted.append(args))
    assert captured["recovery_batch"] == request.recovery_batch.model_dump(mode="json")
    assert "source" not in captured
    assert accepted == [(state, request, "web_search")]
    assert update["user_input_response"].batch_digest == request.recovery_batch.digest
    # 没有受信执行上下文时不能静默接受生产批次决定。
    with pytest.raises(user_input_gate.UserInputResumeError) as exc_info:
        user_input_gate.user_input_gate_node(state)
    assert exc_info.value.code == "permission_recovery_context_required"
