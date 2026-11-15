"""以真实 PostgreSQL 行锁重复关键 HTTP 生命周期回归；只操作随机测试 schema。"""

import os
from functools import partial
from uuid import uuid4

import psycopg
import pytest
from app.api import approvals, sessions
from app.graph import runtime
from app.tests.integration import test_durable_approval_api as approval_cases
from app.tests.integration import test_durable_run_api as cases
from app.tests.integration import test_session_api as helper
from app.tests.support.workflow_llm import WorkflowTestLlm
from app.tools.registry import ToolRegistry
from langgraph.checkpoint.memory import InMemorySaver
from psycopg import sql
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url


@pytest.fixture
def isolated_postgres(monkeypatch):
    url = os.getenv("BS_AGENT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("未配置隔离 PostgreSQL 会话测试地址。")
    schema = f"turn_api_{uuid4().hex}"
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    isolated_url = (
        make_url(url)
        .set(drivername="postgresql+psycopg")
        .update_query_dict(
            {"options": f"-csearch_path={schema} -clock_timeout=3000 -cstatement_timeout=5000"}
        )
    )
    engine = create_engine(isolated_url)
    monkeypatch.setattr(helper, "create_engine", lambda *args, **kwargs: engine)
    monkeypatch.setattr(runtime, "get_default_tool_registry", ToolRegistry)
    monkeypatch.setattr(sessions, "build_graph_checkpointer", InMemorySaver)
    monkeypatch.setattr(sessions, "maybe_build_llm_client", WorkflowTestLlm)
    monkeypatch.setattr(approvals, "build_graph_checkpointer", InMemorySaver)
    monkeypatch.setattr(approvals, "maybe_build_llm_client", lambda: None)
    try:
        yield
    finally:
        engine.dispose()
        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.mark.parametrize(
    "scenario",
    [
        cases.test_http_stop_keeps_partial_answer_replays_without_rerun_and_next_turn_works,
        cases.test_resume_new_phase_does_not_replay_prior_interrupted_terminal,
        cases.test_snapshot_failure_rolls_back_turn_and_active_pointer,
        partial(
            approval_cases.test_durable_approval_resume_uses_control_and_publishes_terminal,
            decision="approved",
            terminal="completed",
        ),
        partial(
            approval_cases.test_stop_during_durable_approval_does_not_release_until_safe_boundary,
            finish_tool=True,
        ),
        approval_cases.test_durable_approval_failure_remains_retryable,
        approval_cases.test_durable_approval_does_not_bypass_unknown_write_barrier,
    ],
)
def test_postgres_http_lifecycle(isolated_postgres, monkeypatch, scenario):
    scenario(monkeypatch)
