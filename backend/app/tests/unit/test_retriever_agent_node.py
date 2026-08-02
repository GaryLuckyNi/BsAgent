import httpx
import pytest

from app.graph.nodes.retriever_agent import build_rag_retriever_node
from app.graph.state import create_initial_state
from app.schemas.approvals import ReadyToolCall
from app.schemas.tools import ToolCallRequest
from app.tools.fingerprints import tool_call_fingerprint
from app.tools.rag_search import RagSearchError, RagSearchTool
from app.tools.registry import ExecutionBinding


TEST_RAG_BINDING = ExecutionBinding(
    provider="rag",
    implementation_id="rag.test.retrieve",
    server_id="rag-test",
    deployment_revision="test-1",
    config_revision="test",
)


def _build_rag_node(tool):
    return build_rag_retriever_node(
        tool=tool,
        execution_binding=TEST_RAG_BINDING,
    )


def _prepare_rag_call(state, *, step_index: int = 0):
    plan = state["plan"] or {}
    requests = list(plan.get("requested_tools", []))
    if requests:
        request = requests[step_index]
        request = request.model_copy(update={"call_id": f"call_rag_{step_index}"})
        requests[step_index] = request
    else:
        request = ToolCallRequest(
            call_id=f"call_rag_{step_index}",
            tool_name="rag_search",
            tool_version="1.0.0",
            arguments={
                "query": state["user_message"],
                "tenant_id": state["tenant_id"],
                "user_id": state["user_id"],
                "top_k": 8,
                "retrieval_mode": "hybrid",
            },
            purpose="测试准备后的检索调用",
        )
        requests.append(request)
    state["plan"] = {**plan, "requested_tools": requests}
    state["ready_tool_call"] = ReadyToolCall(
        call_id=request.call_id,
        step_index=step_index,
        request_fingerprint=tool_call_fingerprint(request),
        tool_name=request.tool_name,
        execution_binding_digest=TEST_RAG_BINDING.digest,
    )
    return request


def test_rag_retriever_node_maps_search_results_to_evidence_package():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "query": {"standalone": "差旅报销需要什么？"},
                "contexts": [
                    {
                        "id": "chunk_1",
                        "text": "差旅报销需要提交发票和审批单。",
                        "source": {
                            "sourcePath": "kb://finance",
                            "title": "差旅制度",
                        },
                        "score": {"final": 0.95},
                    }
                ],
                "metadata": {"contextCount": 1, "durationMs": 11},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://rag")
    tool = RagSearchTool(client=client)
    node = _build_rag_node(tool)
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="差旅报销需要什么？",
        permission_scopes=["rag:retrieve"],
    )
    _prepare_rag_call(state)

    result = node(state)

    assert result["evidence"].coverage == "sufficient"
    assert result["evidence"].items[0].content == "差旅报销需要提交发票和审批单。"
    assert result["tool_calls"][0].request.tool_name == "rag_search"
    assert result["tool_calls"][0].request.arguments["retrieval_mode"] == "hybrid"
    assert result["tool_calls"][0].result.latency_ms >= 0
    assert result["tool_calls"][0].result.result["latency_ms"] == 11


def test_rag_retriever_node_executes_skill_requested_tool_call():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = request.read()
        return httpx.Response(
            200,
            json={
                "query": {"standalone": "差旅报销材料"},
                "contexts": [
                    {
                        "id": "chunk_1",
                        "text": "差旅报销需要提交发票。",
                        "source": {
                            "sourcePath": "kb://finance",
                            "title": "差旅制度",
                        },
                        "score": {"final": 0.9},
                    }
                ],
                "metadata": {"contextCount": 1, "durationMs": 9},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://rag")
    tool = RagSearchTool(client=client)
    node = _build_rag_node(tool)
    state = create_initial_state(
        session_id="s1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="差旅报销需要什么？",
        permission_scopes=["rag:retrieve"],
    )
    state["plan"] = {
        "requested_tools": [
            ToolCallRequest(
                tool_name="rag_search",
                tool_version="1.0.0",
                arguments={
                    "query": "差旅报销材料",
                    "tenant_id": "tenant_1",
                    "user_id": "user_1",
                    "top_k": 3,
                    "retrieval_mode": "hybrid",
                },
                purpose="由投研 skill 请求检索",
            )
        ]
    }
    _prepare_rag_call(state)

    result = node(state)

    assert result["tool_calls"][0].request.purpose == "由投研 skill 请求检索"
    assert result["tool_calls"][0].request.arguments["top_k"] == 3
    assert result["evidence"].query_set == ["差旅报销材料"]


def test_rag_retriever_node_enforces_plan_tool_policy():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"contexts": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://rag")
    node = _build_rag_node(RagSearchTool(client=client))
    state = create_initial_state(
        session_id="s1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="差旅报销需要什么？",
        permission_scopes=["rag:retrieve"],
    )
    state["plan"] = {
        "tool_policy": {"allowed_tools": ["another_tool"]},
        "requested_tools": [],
    }
    _prepare_rag_call(state)

    result = node(state)

    assert result["tool_calls"][-1].result.error.code == "tool_not_allowed_by_plan"
    assert result["errors"][-1]["code"] == "tool_not_allowed_by_plan"
    assert result["ready_tool_call"] is None
    assert calls == []


def test_rag_retriever_node_enforces_selected_skill_tool_policy():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"contexts": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://rag")
    node = _build_rag_node(RagSearchTool(client=client))
    state = create_initial_state(
        session_id="s1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="差旅报销需要什么？",
        permission_scopes=["rag:retrieve"],
    )
    state["plan"] = {
        "tool_policy": {"allowed_tools": ["rag_search"]},
        "requested_tools": [],
    }
    state["selected_skill"] = {
        "skill_name": "restricted_skill",
        "skill_version": "1.0.0",
        "status": "ready",
        "allowed_tools": ["another_tool"],
    }
    _prepare_rag_call(state)

    result = node(state)

    assert result["tool_calls"][-1].result.error.code == "tool_not_allowed_by_skill"
    assert result["errors"][-1]["code"] == "tool_not_allowed_by_skill"
    assert result["ready_tool_call"] is None
    assert calls == []


def test_rag_retriever_node_marks_empty_results_as_no_coverage():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "query": {"standalone": "未知制度"},
                "contexts": [],
                "metadata": {"contextCount": 0, "durationMs": 8},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://rag")
    tool = RagSearchTool(client=client)
    node = _build_rag_node(tool)
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="未知制度",
        permission_scopes=["rag:retrieve"],
    )
    _prepare_rag_call(state)

    result = node(state)

    assert result["evidence"].coverage == "none"
    assert result["evidence"].items == []
    assert result["tool_calls"][0].result.status == "success"


def test_rag_retriever_node_maps_rag_404_to_no_coverage():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "No relevant evidence found."})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://rag")
    tool = RagSearchTool(client=client)
    node = _build_rag_node(tool)
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="未知制度",
        permission_scopes=["rag:retrieve"],
    )
    _prepare_rag_call(state)

    result = node(state)

    assert result["evidence"].coverage == "none"
    assert result["tool_calls"][0].result.status == "failed"
    assert result["tool_calls"][0].result.error.code == "rag_no_evidence"


def test_rag_retriever_node_preserves_rag_503_error_metadata():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "No Chroma index found."})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://rag")
    tool = RagSearchTool(client=client)
    node = _build_rag_node(tool)
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="未知制度",
        permission_scopes=["rag:retrieve"],
    )
    _prepare_rag_call(state)

    result = node(state)

    assert result["tool_calls"][-1].result.error.code == "rag_index_unavailable"
    assert result["errors"][-1]["code"] == "rag_index_unavailable"
    assert result["errors"][-1]["http_status"] == 503
    assert result["ready_tool_call"] is None


def test_rag_retriever_node_downgrades_optional_equity_research_rag_failure():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "No Chroma index found."})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://rag")
    tool = RagSearchTool(client=client)
    node = _build_rag_node(tool)
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="分析一下银之杰",
        permission_scopes=["rag:retrieve"],
    )
    state["plan"] = {
        "retrieval": {
            "required": True,
            "optional": True,
            "failure_policy": "continue_with_structured_evidence",
        },
        "response_policy": {"response_type": "equity_research_report"},
    }
    _prepare_rag_call(state)

    result = node(state)

    assert result["tool_calls"][-1].result.error.code == "rag_index_unavailable"
    assert result["errors"] == []
    assert result["evidence"] is None
    assert result["ready_tool_call"] is None


def test_retriever_requires_explicit_ready_rag_call():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"contexts": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://rag")
    node = _build_rag_node(RagSearchTool(client=client))
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="差旅报销需要什么？",
        permission_scopes=["rag:retrieve"],
    )

    with pytest.raises(RagSearchError) as exc_info:
        node(state)

    assert exc_info.value.code == "rag_call_not_ready"
    assert calls == []


def test_retriever_executes_only_ready_call_id_and_clears_ready_state():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = request.read()
        return httpx.Response(
            200,
            json={
                "query": {"standalone": "ready query"},
                "contexts": [],
                "metadata": {"contextCount": 0, "durationMs": 1},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://rag")
    node = _build_rag_node(RagSearchTool(client=client))
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="原始问题",
        permission_scopes=["rag:retrieve"],
    )
    state["plan"] = {
        "tool_policy": {"allowed_tools": ["rag_search"]},
        "requested_tools": [
            ToolCallRequest(
                call_id="call_first",
                tool_name="rag_search",
                arguments={
                    "query": "wrong query",
                    "tenant_id": "t1",
                    "user_id": "u1",
                    "top_k": 2,
                },
                purpose="不应执行",
            ),
            ToolCallRequest(
                call_id="call_ready",
                tool_name="rag_search",
                arguments={
                    "query": "ready query",
                    "tenant_id": "t1",
                    "user_id": "u1",
                    "top_k": 3,
                },
                purpose="当前可执行检索",
            ),
        ],
    }
    state["ready_tool_call"] = ReadyToolCall(
        call_id="call_ready",
        step_index=1,
        request_fingerprint=tool_call_fingerprint(
            state["plan"]["requested_tools"][1]
        ),
        tool_name="rag_search",
        execution_binding_digest=TEST_RAG_BINDING.digest,
    )

    result = node(state)

    assert b"ready query" in captured["payload"]
    assert b"wrong query" not in captured["payload"]
    assert result["tool_calls"][0].call_id == "call_ready"
    assert result["ready_tool_call"] is None
