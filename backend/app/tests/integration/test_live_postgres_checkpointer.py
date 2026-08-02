import os
from typing import TypedDict
from uuid import uuid4

import pytest
from langgraph.graph import END, START, StateGraph
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.conversations.repository import ConversationRepository
from app.conversations.service import BeginTurn, ConversationService
from app.graph.checkpoints import (
    build_graph_checkpointer,
    close_graph_checkpointer,
)
from app.graph.state import build_checkpoint_thread_id
from app.operations.production_gate import (
    BusinessGateCounts,
    inspect_business_database,
    inspect_legacy_checkpoint_coordinates,
)
from app.storage.models import Base


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


def test_live_postgres_isolates_same_external_session_across_tenants():
    postgres_url = os.getenv("BS_AGENT_TEST_POSTGRES_URL") or os.getenv(
        "BS_AGENT_TEST_POSTGRES_CHECKPOINT_URL"
    )
    if not postgres_url:
        pytest.skip("未配置真实 PostgreSQL Session/Checkpoint 隔离测试地址。")

    schema_name = f"isolation_{uuid4().hex}"
    admin_url = make_url(postgres_url).set(drivername="postgresql+psycopg")
    admin_engine = create_engine(admin_url)
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))

    isolated_url = admin_url.update_query_dict(
        {"options": f"-csearch_path={schema_name}"}
    )
    engine = create_engine(isolated_url)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    checkpoint_url = isolated_url.set(drivername="postgresql").render_as_string(
        hide_password=False
    )
    checkpointer = build_graph_checkpointer(
        backend="postgres",
        postgres_url=checkpoint_url,
    )

    builder = StateGraph(CounterState)
    builder.add_node("increment", lambda state: {"value": state["value"] + 1})
    builder.add_edge(START, "increment")
    builder.add_edge("increment", END)
    graph = builder.compile(checkpointer=checkpointer)

    handles = []
    thread_ids = []
    try:
        for index, tenant_id in enumerate(("tenant_a", "tenant_b"), start=1):
            with session_factory.begin() as session:
                service = ConversationService(session)
                handle = service.begin_turn(
                    BeginTurn(
                        tenant_id=tenant_id,
                        user_id="shared-user",
                        session_id="shared-session",
                        request_id="shared-request",
                        user_content=f"tenant-content-{tenant_id}",
                    )
                )
                service.complete_turn(
                    tenant_id=tenant_id,
                    run_id=handle.run_id,
                    content=f"tenant-answer-{tenant_id}",
                )
                handles.append(handle)

            thread_id = build_checkpoint_thread_id(
                tenant_id=tenant_id,
                session_key=handle.session_key,
                request_id=handle.request_id,
            )
            thread_ids.append(thread_id)
            graph.invoke(
                {"value": index},
                config={"configurable": {"thread_id": thread_id}},
            )

        assert handles[0].session_key != handles[1].session_key
        assert handles[0].run_id != handles[1].run_id
        assert thread_ids[0] != thread_ids[1]
        assert all(thread_id.startswith("cpr1_") for thread_id in thread_ids)
        for index, thread_id in enumerate(thread_ids, start=1):
            snapshot = graph.get_state(
                {"configurable": {"thread_id": thread_id}}
            )
            assert snapshot.values == {"value": index + 1}

        with session_factory() as session:
            histories = []
            for tenant_id, handle in zip(
                ("tenant_a", "tenant_b"), handles, strict=True
            ):
                histories.append(
                    ConversationRepository(session).list_message_page(
                        tenant_id=tenant_id,
                        user_id="shared-user",
                        session_key=handle.session_key,
                        limit=20,
                    )
                )
        assert [[message.content for message in history] for history in histories] == [
            ["tenant-content-tenant_a", "tenant-answer-tenant_a"],
            ["tenant-content-tenant_b", "tenant-answer-tenant_b"],
        ]
        with session_factory() as session:
            assert inspect_business_database(session) == BusinessGateCounts(
                running_runs=0,
                interrupted_runs=0,
                pending_approvals=0,
                legacy_inflight_approvals=0,
            )

        assert inspect_legacy_checkpoint_coordinates(checkpoint_url) == 0
        legacy_thread_id = f"legacy-session-{uuid4().hex}"
        graph.invoke(
            {"value": 10},
            config={"configurable": {"thread_id": legacy_thread_id}},
        )
        assert inspect_legacy_checkpoint_coordinates(checkpoint_url) == 1
        checkpointer.delete_thread(legacy_thread_id)
        assert inspect_legacy_checkpoint_coordinates(checkpoint_url) == 0
    finally:
        close_graph_checkpointer()
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema_name}" CASCADE'))
        admin_engine.dispose()
