import logging
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from langgraph.runtime import Runtime
from langgraph.store.base import Item

from app.graph.nodes.planner_agent import build_planner_agent_node
from app.graph.nodes.synthesizer import build_synthesizer_node
from app.graph.state import create_initial_state
from app.memory.preferences import decode_user_preference, recall_user_preferences
from app.tests.support.workflow_llm import WorkflowTestLlm


def _item(value, *, key: str = "preference") -> Item:
    now = datetime.now(UTC)
    return Item(
        value=value,
        key=key,
        namespace=("tenants", "tenant-1", "users", "user-1", "experiences"),
        created_at=now,
        updated_at=now,
    )


def _state(*, message: str = "你好"):
    return create_initial_state(
        session_id="preference-inputs",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message=message,
    )


@pytest.mark.parametrize("value", [None, "invalid", ["invalid"], 42])
def test_decode_user_preference_ignores_non_mapping_values(value):
    assert decode_user_preference(_item(value)) is None


def test_recall_user_preferences_keeps_valid_items_among_malformed_values():
    store = Mock()
    store.search.return_value = [
        _item(None, key="bad-none"),
        _item(
            {
                "memory_type": "user_preference",
                "memory_key": "language",
                "content": "使用中文回答",
            },
            key="language",
        ),
        _item(["bad-list"], key="bad-list"),
    ]

    preferences = recall_user_preferences(Runtime(store=store), _state())

    assert [(item.memory_key, item.content) for item in preferences] == [
        ("language", "使用中文回答")
    ]


def test_recall_user_preferences_safely_handles_search_failure(caplog):
    caplog.set_level(logging.WARNING, logger="app.memory.preferences")
    store = Mock()
    store.search.side_effect = RuntimeError("postgresql://secret@internal/db")
    failures = []
    state = _state()

    preferences = recall_user_preferences(
        Runtime(store=store),
        state,
        on_failure=failures.append,
    )

    assert preferences == []
    assert failures == [state]
    assert caplog.records[-1].event == "experience_memory_recall_failed"
    assert "postgresql://secret@internal/db" not in caplog.text
    assert state["errors"] == []


class _FailingItems:
    def __iter__(self):
        yield _item(
            {
                "memory_type": "user_preference",
                "memory_key": "language",
                "content": "使用中文回答",
            }
        )
        raise RuntimeError("iteration-secret")


@pytest.mark.parametrize("node_name", ["planner", "synthesizer"])
def test_nodes_continue_when_preference_iteration_fails(node_name, caplog):
    caplog.set_level(logging.WARNING, logger="app.memory.preferences")
    store = Mock()
    store.search.return_value = _FailingItems()
    state = _state()
    state["intent"] = {
        "intent_type": "financial_research",
        "domain": "a_share_equity_research",
        "subdomain": "company_snapshot",
        "goal": "分析海能达",
        "confidence": 1.0,
    }
    if node_name == "planner":
        result = build_planner_agent_node(llm_client=WorkflowTestLlm())(
            state,
            Runtime(store=store),
        )
        assert result["plan"] is not None
    else:
        state["plan"] = {
            "tasks": [
                {"id": "answer", "task_goal": "分析海能达", "depends_on": []}
            ]
        }
        state["task_results"] = [
            {"task_id": "answer", "status": "success", "content": "数据不足"}
        ]
        result = build_synthesizer_node(llm_client=WorkflowTestLlm())(
            state,
            Runtime(store=store),
        )
        assert result["draft_answer"] is not None

    assert caplog.records[-1].event == "experience_memory_recall_failed"
    assert "iteration-secret" not in caplog.text
    assert state["errors"] == []
