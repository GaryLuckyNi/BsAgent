from app.graph.runtime import run_question_answer_graph
from app.memory.preferences import experience_namespace
from app.tests.support.postgres_store import LivePostgresStore
from app.tools.registry import ToolRegistry


pytest_plugins = ("app.tests.support.postgres_store",)


def _put_preference(store, tenant_id: str, user_id: str, key: str, content: str) -> None:
    store.put(
        experience_namespace(tenant_id, user_id),
        key,
        {
            "memory_type": "user_preference",
            "memory_key": key,
            "content": content,
        },
    )


def test_graph_reads_runtime_preferences_and_isolates_identities(
    live_postgres_store: LivePostgresStore,
):
    store = live_postgres_store.store
    _put_preference(store, "tenant-a", "user-a", "language", "使用英文回答")
    _put_preference(store, "tenant-a", "user-a", "response_detail", "回答简洁")
    _put_preference(store, "tenant-b", "user-a", "language", "使用日文回答")
    _put_preference(store, "tenant-a", "user-b", "language", "使用法文回答")

    state = run_question_answer_graph(
        session_id="memory-runtime",
        tenant_id="tenant-a",
        user_id="user-a",
        user_message="你好",
        store=store,
        tool_registry_factory=ToolRegistry,
    )

    assert state["plan"]["response_policy"]["language"] == "english"
    assert state["draft_answer"]["response_policy"]["language"] == "english"
    assert state["draft_answer"]["response_policy"]["detail"] == "concise"
    assert "日文" not in state["draft_answer"]["content"]
    assert "法文" not in state["draft_answer"]["content"]
