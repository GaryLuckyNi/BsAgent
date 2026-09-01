import os
from hashlib import sha256
from typing import TypedDict
from uuid import uuid4

import pytest
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.tests.support.postgres_store import _build_isolated_dsn


class ApprovalState(TypedDict):
    approval_id: str
    action_digest: str
    decision: str | None


def test_live_postgres_interrupt_approval_lifecycle():
    postgres_url = os.getenv("BS_AGENT_TEST_POSTGRES_URL")
    if not postgres_url:
        pytest.skip("未配置真实 PostgreSQL 审批中断测试地址。")

    import psycopg
    from psycopg import sql

    schema = f"approval_interrupt_{uuid4().hex}"
    with psycopg.connect(postgres_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        dsn = _build_isolated_dsn(postgres_url, schema)
        with PostgresSaver.from_conn_string(dsn) as saver:
            saver.setup()
            graph = _approval_graph().compile(checkpointer=saver)
            action_digest = sha256(b"trade:600519").hexdigest()
            config = {"configurable": {"thread_id": f"cpr1_{uuid4().hex}"}}
            state = graph.invoke(
                {"approval_id": "approval_live", "action_digest": action_digest, "decision": None},
                config=config,
            )
            snapshot = graph.get_state(config)
            item = snapshot.tasks[0].interrupts[0]
            checkpoint_id = snapshot.config["configurable"]["checkpoint_id"]
            assert state["__interrupt__"][0].id == item.id
            assert item.value["action_digest"] == action_digest

            exact = {"configurable": {**snapshot.config["configurable"], "checkpoint_id": checkpoint_id}}
            resumed = graph.invoke(
                Command(resume={"approval_id": "approval_live", "action_digest": action_digest, "decision": "approved"}),
                config=exact,
            )
            assert resumed["decision"] == "approved"
            for field, value in (("checkpoint_id", "wrong"), ("interrupt_id", "wrong"), ("action_digest", "wrong")):
                with pytest.raises(ValueError):
                    _validate_resume_target(snapshot, checkpoint_id=checkpoint_id if field != "checkpoint_id" else value, interrupt_id=item.id if field != "interrupt_id" else value, action_digest=action_digest if field != "action_digest" else value)
    finally:
        with psycopg.connect(postgres_url, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _approval_graph():
    graph = StateGraph(ApprovalState)
    def gate(state: ApprovalState):
        decision = interrupt({"approval_id": state["approval_id"], "action_digest": state["action_digest"], "approval_type": "tool_call", "subject_name": "local.trade", "risk_level": "high", "arguments": {"symbol": "600519"}})
        return {"decision": decision["decision"]}
    graph.add_node("gate", gate)
    graph.add_edge(START, "gate")
    graph.add_edge("gate", END)
    return graph


def _validate_resume_target(snapshot, *, checkpoint_id: str, interrupt_id: str, action_digest: str) -> None:
    if snapshot.config["configurable"]["checkpoint_id"] != checkpoint_id:
        raise ValueError("checkpoint mismatch")
    item = snapshot.tasks[0].interrupts[0]
    if item.id != interrupt_id or item.value["action_digest"] != action_digest:
        raise ValueError("interrupt mismatch")
