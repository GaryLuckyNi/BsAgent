"""生产恢复入口必须先拒绝身份或展示协议不符，再考虑执行 Graph。"""

from types import SimpleNamespace

import pytest
from app.graph import runtime
from app.graph.state import build_checkpoint_thread_id, create_initial_state
from app.tests.unit.test_permission_fallback import setup_worker
from app.tests.unit.test_user_input_schemas import batch_request


@pytest.mark.parametrize(
    "field",
    ["tenant_id", "user_id", "session_id", "session_key", "request_id", "assistant_message_id"],
)
def test_identity_mismatch_rejects_before_graph_invoke(monkeypatch, field):
    _, state, _, search, _, registry = setup_worker()
    invoked = []
    graph = SimpleNamespace(
        get_state=lambda config: SimpleNamespace(values=state),
        invoke=lambda *args, **kwargs: invoked.append(args),
    )
    monkeypatch.setattr(runtime, "build_question_answer_graph", lambda **kwargs: graph)
    with pytest.raises(runtime.AgentGraphExecutionError) as exc:
        runtime.resume_user_input_graph(
            business_intent_catalog=object(),
            checkpointer=None,
            checkpoint_thread_id=build_checkpoint_thread_id(
                tenant_id=state["tenant_id"],
                session_key=state["session_key"],
                request_id=state["request_id"],
            ),
            interrupt_id="interrupt",
            interaction_id="interaction",
            input_type="selection",
            value="web_search",
            tool_registry_factory=lambda: registry,
            expected_identity={field: "different"},
        )
    assert exc.value.code == "user_input_resume_mismatch"
    assert invoked == [] and search.calls == 0


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"presentation_version": "old", "batch_digest": "a" * 64},
        {"presentation_version": "permission-recovery-batch-v1", "batch_digest": "b" * 64},
    ],
)
def test_batch_presentation_rejects_before_graph_invoke(monkeypatch, metadata):
    _, _, _, search, _, registry = setup_worker()
    state = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="查询")
    request = batch_request()
    payload = {
        "interrupt_type": "user_input",
        "interaction_id": request.interaction_id,
        "input_type": "selection",
        "recovery_batch": request.recovery_batch.model_dump(mode="json"),
    }
    snapshot = SimpleNamespace(
        values=state,
        tasks=[SimpleNamespace(interrupts=[SimpleNamespace(id="interrupt", value=payload)])],
    )
    invoked = []
    graph = SimpleNamespace(
        get_state=lambda config: snapshot, invoke=lambda *args, **kwargs: invoked.append(args)
    )
    monkeypatch.setattr(runtime, "build_question_answer_graph", lambda **kwargs: graph)
    with pytest.raises(runtime.AgentGraphExecutionError) as exc:
        runtime.resume_user_input_graph(
            business_intent_catalog=object(),
            checkpointer=None,
            checkpoint_thread_id=build_checkpoint_thread_id(
                tenant_id="t", session_key=state["session_key"], request_id=state["request_id"]
            ),
            interrupt_id="interrupt",
            interaction_id=request.interaction_id,
            input_type="selection",
            value="web_search",
            tool_registry_factory=lambda: registry,
            **metadata,
        )
    assert exc.value.code == "permission_recovery_presentation_mismatch"
    assert invoked == [] and search.calls == 0


def test_public_protocol_preserves_only_validated_complete_view():
    from app.api.sessions import _ProtocolUserInputResume, _public_user_input_interrupt

    request = batch_request()
    payload = {
        "interrupt_type": "user_input",
        "interrupt_id": "interrupt",
        "interaction_id": request.interaction_id,
        "input_type": "selection",
        "question": request.question,
        "options": [item.model_dump(mode="json") for item in request.options],
        "recovery_batch": request.recovery_batch.model_dump(mode="json"),
        "private_context": "not public",
    }
    public = _public_user_input_interrupt(payload)
    assert public["recovery_batch"] == payload["recovery_batch"]
    assert "private_context" not in public
    resume = _ProtocolUserInputResume.model_validate(
        {
            "interrupt_id": "interrupt",
            "interaction_id": request.interaction_id,
            "input_type": "selection",
            "value": "web_search",
            "presentation_version": request.recovery_batch.version,
            "batch_digest": request.recovery_batch.digest,
        }
    )
    assert resume.batch_digest == request.recovery_batch.digest


@pytest.mark.parametrize("change", ["items", "missing", "version"])
def test_incomplete_or_changed_server_presentation_is_rejected_before_invoke(monkeypatch, change):
    from copy import deepcopy

    from app.schemas.recovery import RECOVERY_VERSION

    _, _, _, search, _, registry = setup_worker()
    state = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="查询")
    request = batch_request()
    state["pending_user_input"] = request
    state["recovery_policy_version"] = RECOVERY_VERSION
    presentation = deepcopy(request.recovery_batch.model_dump(mode="json"))
    if change == "items":
        presentation["items"].pop()
    elif change == "version":
        state["recovery_policy_version"] = "unsupported"
    payload = {
        "interrupt_type": "user_input",
        "interaction_id": request.interaction_id,
        "input_type": "selection",
    }
    if change != "missing":
        payload["recovery_batch"] = presentation
    snapshot = SimpleNamespace(
        values=state,
        tasks=[SimpleNamespace(interrupts=[SimpleNamespace(id="interrupt", value=payload)])],
    )
    invoked = []
    graph = SimpleNamespace(
        get_state=lambda config: snapshot, invoke=lambda *args, **kwargs: invoked.append(args)
    )
    monkeypatch.setattr(runtime, "build_question_answer_graph", lambda **kwargs: graph)
    with pytest.raises(runtime.AgentGraphExecutionError) as exc:
        runtime.resume_user_input_graph(
            business_intent_catalog=object(),
            checkpointer=None,
            checkpoint_thread_id=build_checkpoint_thread_id(
                tenant_id="t", session_key=state["session_key"], request_id=state["request_id"]
            ),
            interrupt_id="interrupt",
            interaction_id=request.interaction_id,
            input_type="selection",
            value="web_search",
            tool_registry_factory=lambda: registry,
            presentation_version=request.recovery_batch.version,
            batch_digest=request.recovery_batch.digest,
        )
    assert exc.value.http_status == 409
    assert exc.value.code == (
        "permission_recovery_presentation_mismatch"
        if change == "items"
        else "permission_recovery_version_mismatch"
    )
    assert invoked == [] and search.calls == 0
