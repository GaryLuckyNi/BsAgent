import pytest
from app.schemas.user_input import (
    UserInputOption,
    UserInputRequest,
    UserInputResponse,
    UserInputSource,
)
from pydantic import ValidationError


def _source() -> UserInputSource:
    return UserInputSource(
        node="tool_agent",
        task_id="resolve_entity",
        call_id="call_1",
        kind="entity_disambiguation",
    )


def _options() -> list[UserInputOption]:
    return [
        UserInputOption(value="600150.SH", label="中国船舶", description="上交所"),
        UserInputOption(value="600685.SH", label="中船防务", description="上交所"),
    ]


def test_selection_request_and_response_are_strict_and_frozen():
    request = UserInputRequest(
        interaction_id="interaction_1",
        input_type="selection",
        question="请选择本次研究的标的",
        options=_options(),
        source=_source(),
    )
    response = UserInputResponse(
        interaction_id=request.interaction_id,
        input_type="selection",
        value="600150.SH",
        source=request.source,
    )

    assert response.source.kind == "entity_disambiguation"
    with pytest.raises(ValidationError):
        request.question = "不允许修改"
    with pytest.raises(ValidationError):
        UserInputOption(value="1", label="一", unknown="field")


@pytest.mark.parametrize(
    "options",
    [
        [UserInputOption(value="only", label="唯一")],
        [
            UserInputOption(value="same", label="候选一"),
            UserInputOption(value="same", label="候选二"),
        ],
    ],
)
def test_selection_request_rejects_too_few_or_duplicate_options(options):
    with pytest.raises(ValidationError):
        UserInputRequest(
            interaction_id="interaction_1",
            input_type="selection",
            question="请选择",
            options=options,
            source=_source(),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("interaction_id", ""),
        ("question", " "),
        ("question", "问" * 501),
    ],
)
def test_selection_request_rejects_empty_or_oversized_text(field, value):
    payload = {
        "interaction_id": "interaction_1",
        "input_type": "selection",
        "question": "请选择",
        "options": _options(),
        "source": _source(),
    }
    payload[field] = value

    with pytest.raises(ValidationError):
        UserInputRequest(**payload)


def test_first_version_rejects_unimplemented_input_types():
    with pytest.raises(ValidationError):
        UserInputRequest(
            interaction_id="interaction_1",
            input_type="text",
            question="请补充",
            options=_options(),
            source=_source(),
        )


def batch_view(count=2):
    from app.schemas.recovery import RecoveryBatchView

    return RecoveryBatchView(
        batch_id="recovery_1", digest="a" * 64,
        items=[{
            "item_id": f"c{i}", "task_goal": f"研究目标{i}", "original_tool": "mcp.x.api",
            "reason": "数据源账号权限不足", "source_notice": "公开来源不等价于原接口数据",
        } for i in range(count)],
    )


def batch_request():
    return UserInputRequest(
        interaction_id="interaction_batch", input_type="selection", question="是否替代取证？",
        options=[UserInputOption(value="web_search", label="对以上任务使用联网搜索"),
                 UserInputOption(value="use_existing", label="仅使用已有资料")],
        source=UserInputSource(node="task_scheduler", kind="provider_permission_recovery_batch"),
        recovery_batch=batch_view(),
    )


def test_batch_source_cannot_masquerade_as_single_call_or_omit_view():
    data = batch_request().model_dump(mode="json")
    data["source"]["call_id"] = "c0"
    with pytest.raises(ValidationError):
        UserInputRequest.model_validate(data)
    data["source"]["call_id"] = None
    data["recovery_batch"] = None
    with pytest.raises(ValidationError):
        UserInputRequest.model_validate(data)


def test_public_batch_rejects_oversized_or_duplicate_items_and_unknown_version():
    from app.schemas.recovery import RecoveryBatchView

    with pytest.raises(ValidationError):
        batch_view(13)
    data = batch_view().model_dump(mode="json")
    data["items"][1]["item_id"] = data["items"][0]["item_id"]
    with pytest.raises(ValidationError):
        RecoveryBatchView.model_validate(data)
    data = batch_view().model_dump(mode="json") | {"version": "future"}
    with pytest.raises(ValidationError):
        RecoveryBatchView.model_validate(data)
