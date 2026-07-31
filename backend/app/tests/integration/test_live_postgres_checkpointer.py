import os
from typing import TypedDict
from uuid import uuid4

import pytest
from langgraph.graph import END, START, StateGraph

from app.graph.checkpoints import (
    build_graph_checkpointer,
    close_graph_checkpointer,
)


class CounterState(TypedDict):
    value: int


def test_live_postgres_saver_persists_and_reads_graph_state():
    postgres_url = os.getenv("BS_AGENT_TEST_POSTGRES_CHECKPOINT_URL")
    if not postgres_url:
        pytest.skip("未配置真实 PostgreSQL Checkpoint 测试地址。")

    builder = StateGraph(CounterState)
    builder.add_node("increment", lambda state: {"value": state["value"] + 1})
    builder.add_edge(START, "increment")
    builder.add_edge("increment", END)
    checkpointer = build_graph_checkpointer(
        backend="postgres",
        postgres_url=postgres_url,
    )
    graph = builder.compile(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": f"e2e-{uuid4().hex}"}}

    try:
        result = graph.invoke({"value": 40}, config=config)
        snapshot = graph.get_state(config)
    finally:
        close_graph_checkpointer()

    assert result == {"value": 41}
    assert snapshot.values == {"value": 41}
