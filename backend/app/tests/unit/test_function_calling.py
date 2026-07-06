import pytest

from app.tools.function_calling import FunctionCallAdapter


def test_function_calling_adapter_parses_json_arguments_into_tool_request():
    request = FunctionCallAdapter().to_tool_call_request(
        {
            "name": "rag_search",
            "arguments": '{"query": "报销制度", "top_k": 3}',
        },
        purpose="检索制度依据",
    )

    assert request.tool_name == "rag_search"
    assert request.arguments == {"query": "报销制度", "top_k": 3}
    assert request.purpose == "检索制度依据"


def test_function_calling_adapter_accepts_dict_arguments():
    request = FunctionCallAdapter().to_tool_call_request(
        {
            "function": {
                "name": "rag_search",
                "arguments": {"query": "报销制度"},
            }
        },
        purpose="检索制度依据",
    )

    assert request.tool_name == "rag_search"
    assert request.arguments == {"query": "报销制度"}


def test_function_calling_adapter_rejects_invalid_json_arguments():
    with pytest.raises(ValueError, match="Invalid function call arguments"):
        FunctionCallAdapter().to_tool_call_request(
            {"name": "rag_search", "arguments": "{bad json"},
            purpose="检索制度依据",
        )
