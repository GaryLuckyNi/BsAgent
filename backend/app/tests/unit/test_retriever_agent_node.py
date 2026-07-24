import httpx

from app.graph.nodes.retriever_agent import build_rag_retriever_node
from app.graph.state import create_initial_state
from app.schemas.tools import ToolCallRequest
from app.tools.rag_search import RagSearchTool


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
    node = build_rag_retriever_node(tool=tool)
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="差旅报销需要什么？",
        permission_scopes=["rag:retrieve"],
    )

    result = node(state)

    assert result["evidence"].coverage == "sufficient"
    assert result["evidence"].items[0].content == "差旅报销需要提交发票和审批单。"
    assert result["tool_calls"][0].request.tool_name == "rag_search"
    assert result["tool_calls"][0].request.arguments["retrieval_mode"] == "hybrid"
    assert result["tool_calls"][0].result.latency_ms == 11


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
    node = build_rag_retriever_node(tool=tool)
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
                purpose="由 policy_qa skill 请求检索",
            )
        ]
    }

    result = node(state)

    assert result["tool_calls"][0].request.purpose == "由 policy_qa skill 请求检索"
    assert result["tool_calls"][0].request.arguments["top_k"] == 3
    assert result["evidence"].query_set == ["差旅报销材料"]


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
    node = build_rag_retriever_node(tool=tool)
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="未知制度",
        permission_scopes=["rag:retrieve"],
    )

    result = node(state)

    assert result["evidence"].coverage == "none"
    assert result["evidence"].items == []
    assert result["tool_calls"][0].result.status == "success"


def test_rag_retriever_node_maps_rag_404_to_no_coverage():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "No relevant evidence found."})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://rag")
    tool = RagSearchTool(client=client)
    node = build_rag_retriever_node(tool=tool)
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="未知制度",
        permission_scopes=["rag:retrieve"],
    )

    result = node(state)

    assert result["evidence"].coverage == "none"
    assert result["tool_calls"][0].result.status == "failed"
    assert result["tool_calls"][0].result.error.code == "rag_no_evidence"


def test_rag_retriever_node_preserves_rag_503_error_metadata():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "No Chroma index found."})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://rag")
    tool = RagSearchTool(client=client)
    node = build_rag_retriever_node(tool=tool)
    state = create_initial_state(
        session_id="s1",
        tenant_id="t1",
        user_id="u1",
        user_message="未知制度",
        permission_scopes=["rag:retrieve"],
    )

    try:
        node(state)
    except RuntimeError as exc:
        assert exc.code == "rag_index_unavailable"
        assert exc.http_status == 503
    else:
        raise AssertionError("expected rag retriever to raise structured error")
