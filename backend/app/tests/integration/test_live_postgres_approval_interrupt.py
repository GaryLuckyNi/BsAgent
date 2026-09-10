import os
import json

import pytest
from langgraph.checkpoint.postgres import PostgresSaver
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.api import sessions
from app.approvals.repository import ApprovalRepository
from app.graph.nodes.approval_gate import ApprovalPayloadMismatchError
from app.graph.runtime import resume_question_answer_graph, run_question_answer_graph
from app.llm.schemas import LlmCompletionResult
from app.storage.models import ApprovalRecord, Base
from app.tests.support.postgres_store import (
    LivePostgresStore,
    open_isolated_postgres_store,
)
from app.tools.registry import ToolRegistry


class LocalPreferenceLlm:
    async def complete(self, **kwargs):
        prompt = kwargs.get("system_prompt", "")
        if "投研任务理解器" in prompt:
            content = json.dumps(
                {
                    "intent_type": "financial_research",
                    "domain": "a_share_equity_research",
                    "subdomain": "company_snapshot",
                    "goal": "分析海能达并记住用户明确提供的联系方式",
                    "confidence": 0.99,
                },
                ensure_ascii=False,
            )
        elif "投研任务 Planner" in prompt:
            content = json.dumps(
                {
                    "tasks": [
                        {
                            "id": "analyze_company",
                            "task_goal": "基于已有上下文分析海能达",
                            "depends_on": [],
                            "skill_name": None,
                            "tool_name": None,
                        }
                    ]
                },
                ensure_ascii=False,
            )
        elif "投研任务执行器" in prompt:
            content = "未提供外部数据，本任务仅验证审批恢复。"
        elif "投研报告合成器" in prompt:
            content = "## 处理结果\n\n偏好审批已处理。"
        else:
            raise AssertionError(prompt)
        return LlmCompletionResult(
            content=content,
            model="local-test",
            provider="local",
        )


def test_live_postgres_application_approval_lifecycle(
    business_intent_catalog,
    monkeypatch,
):
    postgres_url = os.getenv("BS_AGENT_TEST_POSTGRES_URL")
    if not postgres_url:
        pytest.skip("未配置真实 PostgreSQL 审批中断测试地址。")
    with open_isolated_postgres_store(postgres_url) as live_postgres_store:
        _exercise_application_lifecycle(
            live_postgres_store,
            monkeypatch,
            business_intent_catalog,
        )


def _exercise_application_lifecycle(
    live_postgres_store: LivePostgresStore,
    monkeypatch,
    business_intent_catalog,
):
    business_engine = create_engine(
        make_url(live_postgres_store.dsn).set(drivername="postgresql+psycopg")
    )
    Base.metadata.create_all(business_engine)
    business_sessions = sessionmaker(bind=business_engine, expire_on_commit=False)

    with PostgresSaver.from_conn_string(live_postgres_store.dsn) as saver:
        saver.setup()
        monkeypatch.setattr(sessions, "build_graph_checkpointer", lambda: saver)
        state = _run_interrupted_graph(saver, business_intent_catalog)
        projected = sessions._interrupt_payloads(state)

        assert len(projected) == 1
        payload = projected[0]
        assert payload["checkpoint_id"]
        assert payload["interrupt_id"] == state["__interrupt__"][0].id
        assert payload["action_digest"] == state["__interrupt__"][0].value["action_digest"]

        with business_sessions.begin() as db:
            record = ApprovalRepository(db).create_pending_from_interrupt(payload)
        with business_sessions() as db:
            persisted = db.scalar(select(ApprovalRecord))
            assert persisted is not None
            assert {
                "approval_id": persisted.approval_id,
                "run_id": persisted.run_id,
                "tenant_id": persisted.tenant_id,
                "approval_type": persisted.approval_type,
                "subject_name": persisted.subject_name,
                "risk_level": persisted.risk_level,
                "action_digest": persisted.action_digest,
                "checkpoint_thread_id": persisted.checkpoint_thread_id,
                "checkpoint_namespace": persisted.checkpoint_namespace,
                "checkpoint_id": persisted.checkpoint_id,
                "interrupt_id": persisted.interrupt_id,
                "requested_by": persisted.requested_by,
                "status": persisted.status,
            } == {
                "approval_id": record.approval_id,
                "run_id": payload["run_id"],
                "tenant_id": payload["tenant_id"],
                "approval_type": payload["approval_type"],
                "subject_name": payload["subject_name"],
                "risk_level": payload["risk_level"],
                "action_digest": payload["action_digest"],
                "checkpoint_thread_id": payload["checkpoint_thread_id"],
                "checkpoint_namespace": payload["checkpoint_namespace"],
                "checkpoint_id": payload["checkpoint_id"],
                "interrupt_id": payload["interrupt_id"],
                "requested_by": payload["requested_by"],
                "status": "pending",
            }
            database_columns = {
                column["name"] for column in inspect(db.bind).get_columns("approval_records")
            }
            orm_columns = set(ApprovalRecord.__table__.columns.keys())
            forbidden_columns = {
                "payload",
                "payload_json",
                "arguments",
                "content",
                "session_id",
                "session_key",
                "request_user_id",
                "tool_name",
                "tool_version",
                "arguments_hash",
                "safe_arguments",
                "purpose",
            }
            assert database_columns == orm_columns
            assert database_columns.isdisjoint(forbidden_columns)

        resumed = _resume(saver, payload, business_intent_catalog)
        candidate = next(
            item
            for item in resumed["memory_candidates"]
            if item.candidate_id == payload["candidate_id"]
        )
        assert candidate.approval_status == "approved"

        mismatches = (
            {"checkpoint_id": "wrong-checkpoint"},
            {"interrupt_id": "wrong-interrupt"},
            {"action_digest": "wrong-digest"},
        )
        for mismatch in mismatches:
            with pytest.raises(ApprovalPayloadMismatchError):
                _resume(
                    saver,
                    {**payload, **mismatch},
                    business_intent_catalog,
                )

    business_engine.dispose()


def _run_interrupted_graph(saver: PostgresSaver, business_intent_catalog):
    return run_question_answer_graph(
        business_intent_catalog=business_intent_catalog,
        session_id="live-approval-session",
        session_key="live-approval-session-key",
        tenant_id="tenant-live",
        user_id="user-live",
        user_message="分析海能达，并请记住我的联系电话：13800138000",
        request_id="request-live-approval",
        checkpointer=saver,
        tool_registry_factory=ToolRegistry,
        llm_client=LocalPreferenceLlm(),
        skill_agent_node=_local_skill_node,
    )


def _resume(saver: PostgresSaver, payload: dict, business_intent_catalog):
    return resume_question_answer_graph(
        business_intent_catalog=business_intent_catalog,
        checkpoint_thread_id=payload["checkpoint_thread_id"],
        checkpoint_namespace=payload["checkpoint_namespace"],
        checkpoint_id=payload["checkpoint_id"],
        interrupt_id=payload["interrupt_id"],
        action_digest=payload["action_digest"],
        approval_decision={
            "approval_id": payload["approval_id"],
            "action_digest": payload["action_digest"],
            "decision": "approved",
        },
        checkpointer=saver,
        tool_registry_factory=ToolRegistry,
        llm_client=LocalPreferenceLlm(),
        skill_agent_node=_local_skill_node,
    )


def _local_skill_node(state):
    return state
