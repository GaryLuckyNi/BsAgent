import pytest
from pydantic import ValidationError

from app.schemas.user_input import (
    UserInputOption,
    UserInputRequest,
    UserInputResponse,
    UserInputSource,
)


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
