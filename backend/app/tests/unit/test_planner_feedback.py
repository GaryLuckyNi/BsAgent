import json

import pytest
from app.graph.nodes.planner_agent import build_planner_agent_node
from app.graph.planning.contracts import PlanModel, TaskResultModel
from app.graph.planning.planner import LlmTaskPlanner, PlannerCapabilities, PlannerOutputError
from app.graph.state import create_initial_state
from app.llm.schemas import LlmCompletionResult
from pydantic import ValidationError


class Decisions:
    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        payload = self.payloads[min(len(self.calls) - 1, len(self.payloads) - 1)]
        return LlmCompletionResult(content=json.dumps(payload), model="test", provider="test")


class InvalidFeedback:
    def __init__(self):
        self.calls = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        return LlmCompletionResult(content="无法给出有效 JSON", model="test", provider="test")


def plan():
    return PlanModel(
        tasks=[
            {"id": "facts", "task_goal": "核实事实"},
            {
                "id": "analysis",
                "task_goal": "分析",
                "depends_on": ["facts"],
                "execution_type": "analysis",
            },
        ]
    )


def review(client, *, results=None, current=None, remaining=1):
    return LlmTaskPlanner(llm_client=client).review(
        goal="分析原因",
        user_message="只使用提供的资料分析原因",
        current_plan=current or plan(),
        task_results=tuple(
            results or [TaskResultModel(task_id="facts", status="success", content="已取得事实")]
        ),
        capabilities=PlannerCapabilities(),
        remaining_revisions=remaining,
    )


@pytest.mark.parametrize("action", ["continue", "finish", "revise"])
def test_model_chooses_action_for_same_observation(action):
    payload = {"action": action, "reason": "根据证据选择"}
    if action == "revise":
        payload["plan"] = {"tasks": [{"id": "more", "task_goal": "补充证据"}]}
    current = plan()
    if action == "continue":
        current.tasks[1] = current.tasks[1].model_copy(update={
            "execution_type": "tool", "task_goal": "根据事实进一步核实公告",
        })
    decision = review(Decisions(payload), current=current)
    assert decision.action == action


def test_summary_only_legacy_analysis_revision_finishes_without_retry_or_revision_budget():
    current = plan()
    proposed = {**current.model_dump(), "public_summary": "旧版本只换了摘要，执行任务未变"}
    client = Decisions({"action": "revise", "reason": "继续研究", "plan": proposed})

    decision = review(client, current=current, remaining=0)

    assert decision.action == "finish"
    assert decision.plan is None
    assert len(client.calls) == 1


def test_noop_revision_does_not_namespace_tasks_or_increment_iteration():
    state = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="分析原因")
    state.update(
        intent={"intent_type": "research", "domain": "equity", "subdomain": "company", "goal": "分析原因"},
        plan=plan().model_dump(), planner_iteration=1,
        task_results=[{"task_id": "facts", "status": "success", "content": "已取得事实"}],
    )
    client = Decisions({"action": "revise", "reason": "换摘要", "plan": {
        **plan().model_dump(), "public_summary": "新的摘要",
    }})

    result = build_planner_agent_node(llm_client=client)(state)

    assert result["planner_iteration"] == 1
    assert result["plan_history"] == []
    assert [task["id"] for task in result["plan"]["tasks"]] == ["facts", "analysis"]
    assert result["planner_decision"]["action"] == "finish"
    assert len(client.calls) == 1


def test_planner_can_explicitly_handoff_ready_terminal_analysis_to_synthesis():
    client = Decisions({
        "action": "finish", "reason": "取证阶段结束，最后的研判由最终节点一次完成",
        "synthesis_task_id": "analysis", "unresolved_questions": ["估值缺口仍需说明"],
    })

    decision = review(client)

    assert decision.synthesis_task_id == "analysis"
    assert decision.action == "finish"
    assert len(client.calls) == 1


def test_finished_plan_drops_redundant_completed_handoff_without_another_model_call():
    current=PlanModel(tasks=[{"id":"concept","task_goal":"解释PE","execution_type":"analysis"}])
    client=Decisions({"action":"finish","reason":"已完成概念解释","synthesis_task_id":"concept"})
    decision=review(client,current=current,results=[TaskResultModel(task_id="concept",status="success",content="PE定义")])
    assert decision.action=="finish" and decision.synthesis_task_id is None
    assert len(client.calls)==1


@pytest.mark.parametrize("case", ["unknown", "tool", "consumer", "failed_dependency", "other_pending"])
def test_synthesis_handoff_cannot_skip_real_work_or_failed_dependencies(case):
    current = plan()
    task_id = "analysis"
    results = [TaskResultModel(task_id="facts", status="success", content="事实")]
    if case == "unknown":
        task_id = "invented"
    elif case == "tool":
        task_id = "facts"
        results = [TaskResultModel(task_id="archived", status="success", content="历史")]
    elif case == "consumer":
        current = PlanModel(tasks=[*current.tasks, {
            "id": "next_tool", "task_goal": "使用分析结果查询", "depends_on": ["analysis"],
        }])
    elif case == "failed_dependency":
        results = [TaskResultModel(task_id="facts", status="business_failure", content="无权限")]
    else:
        current = PlanModel(tasks=[*current.tasks, {"id": "more", "task_goal": "补充取证"}])
    client = Decisions({"action": "finish", "reason": "交给最终回答", "synthesis_task_id": task_id})

    with pytest.raises(PlannerOutputError):
        review(client, current=current, results=results)

    assert len(client.calls) == 2


def test_no_progress_continue_is_returned_to_model_for_repair():
    client = Decisions(
        {"action": "continue", "reason": "继续"},
        {"action": "finish", "reason": "没有可推进任务，说明限制"},
    )
    result = review(
        client,
        results=[TaskResultModel(task_id="facts", status="business_failure", content="无权限")],
    )
    assert result.action == "finish"
    assert len(client.calls) == 2
    assert "可执行" in client.calls[1]["user_prompt"]


def test_revision_cannot_rewrite_completed_task():
    client = Decisions(
        {
            "action": "revise",
            "reason": "修改",
            "plan": {"tasks": [{"id": "facts", "task_goal": "更换目标"}]},
        }
    )
    with pytest.raises(PlannerOutputError):
        review(client)


def test_revision_reuses_successful_removed_dependency_without_model_retry():
    payload = {
        "action": "revise", "reason": "复用事实，补齐缺口",
        "plan": {"tasks": [
            {"id": "more", "task_goal": "补充关键证据"},
            {"id": "verify", "task_goal": "根据新旧事实核实公告", "execution_type": "tool",
             "depends_on": ["more", "facts"], "evidence_task_ids": ["facts"]},
        ]},
    }
    client = Decisions(payload)

    decision = review(client)

    assert len(client.calls) == 1
    assert decision.plan.tasks[1].depends_on == ["more"]
    assert decision.plan.tasks[1].evidence_task_ids == ["facts"]
    assert payload["plan"]["tasks"][1]["depends_on"] == ["more", "facts"]


def test_converted_history_is_available_to_tool_after_active_dependency_completes():
    from app.graph.planning.context import task_evidence
    from app.graph.planning.scheduler import ready_tasks

    decision = review(Decisions({
        "action": "revise", "reason": "复用已有成功证据",
        "plan": {"tasks": [
            {"id": "more", "task_goal": "补充关键证据"},
            {"id": "verify", "task_goal": "根据新旧事实核实公告", "execution_type": "tool",
             "depends_on": ["more", "facts"]},
        ]},
    }))
    results = [TaskResultModel(task_id="facts", status="success", content="历史证据")]
    assert [task.id for task in ready_tasks(decision.plan, results=results)] == ["more"]
    results.append(TaskResultModel(task_id="more", status="success", content="新增证据"))
    assert [task.id for task in ready_tasks(decision.plan, results=results)] == ["verify"]
    evidence = task_evidence(
        {"task_results": [result.model_dump() for result in results]}, decision.plan.tasks[1],
    )
    assert {item["content"] for item in evidence} == {"历史证据", "新增证据"}


def test_successful_dependency_still_in_plan_remains_an_execution_dependency():
    client = Decisions({
        "action": "revise", "reason": "保留成功任务并补充证据",
        "plan": {"tasks": [
            {"id": "facts", "task_goal": "核实事实"},
            {"id": "more", "task_goal": "补充关键证据", "depends_on": ["facts"]},
        ]},
    })
    decision = review(client)
    assert len(client.calls) == 1
    assert decision.plan.tasks[1].depends_on == ["facts"]
    assert decision.plan.tasks[1].evidence_task_ids == []


@pytest.mark.parametrize("tasks", [
    [{"id": "more", "task_goal": "补充关键证据", "depends_on": ["more"]}],
    [{"id": "more", "task_goal": "补充关键证据", "depends_on": ["analysis"]},
     {"id": "analysis", "task_goal": "分析", "depends_on": ["more"]}],
    [{"id": "more", "task_goal": "补充关键证据", "depends_on": ["facts", "facts"]}],
    [{"id": "more", "task_goal": "补充关键证据", "evidence_task_ids": ["facts", "facts"]}],
])
def test_history_conversion_does_not_hide_invalid_task_graph_or_duplicate_references(tasks):
    client = Decisions({"action": "revise", "reason": "补充证据", "plan": {"tasks": tasks}})
    with pytest.raises(PlannerOutputError):
        review(client)


def test_history_dependency_conversion_does_not_mutate_raw_response():
    from copy import deepcopy

    from app.graph.planning.planner import _normalize_successful_history_dependencies

    raw = {"action": "revise", "reason": "复用历史", "plan": {"tasks": [
        {"id": "more", "task_goal": "补充证据", "depends_on": ["facts"]},
    ]}}
    before = deepcopy(raw)
    normalized = _normalize_successful_history_dependencies(
        raw, results=(TaskResultModel(task_id="facts", status="success", content="历史证据"),),
    )

    assert normalized["plan"]["tasks"][0]["depends_on"] == []
    assert normalized["plan"]["tasks"][0]["evidence_task_ids"] == ["facts"]
    assert raw == before


@pytest.mark.parametrize("dependency,status", [
    ("facts", "business_failure"), ("facts", "terminal_failure"), ("invented", "success"),
])
def test_revision_does_not_convert_failed_or_unknown_history_to_evidence(dependency, status):
    client = Decisions({
        "action": "revise", "reason": "补齐缺口",
        "plan": {"tasks": [
            {"id": "more", "task_goal": "补充关键证据"},
            {"id": "analysis", "task_goal": "分析", "execution_type": "analysis",
             "depends_on": ["more", dependency]},
        ]},
    })

    with pytest.raises(PlannerOutputError):
        review(client, results=[TaskResultModel(task_id="facts", status=status, content="结果")])
    assert len(client.calls) == 2


def test_feedback_prompt_exposes_current_datetime_and_successful_history_ids():
    from datetime import datetime

    client = Decisions({"action": "finish", "reason": "已有材料足够"})
    review(client)
    prompt = json.loads(client.calls[0]["user_prompt"])

    assert datetime.fromisoformat(prompt["current_datetime"]).utcoffset().total_seconds() == 8 * 3600
    assert prompt["timezone"] == "Asia/Shanghai"
    assert prompt["successful_history_task_ids"] == ["facts"]


@pytest.mark.parametrize(
    "new_goal,new_query",
    [
        ("查询贵州茅台2023年财报营收与净利润", "股票财务指标查询"),
        ("查询五粮液2024年财报营收与净利润", "股票财务指标查询"),
        ("查询贵州茅台2024年财报营收与经营现金流", "股票财务指标查询"),
        ("查询贵州茅台2023年财报营收与净利润", "历史财务指标查询"),
    ],
)
def test_revision_allows_distinct_period_entity_or_metric_with_same_capability(
    new_goal, new_query,
):
    current = PlanModel(tasks=[{
        "id": "completed", "task_goal": "查询贵州茅台2024年财报营收与净利润",
        "tool_search_query": "股票财务指标查询",
        "completion_criteria": ["提供可核实的财务指标"],
    }])
    client = Decisions({
        "action": "revise", "reason": "需要可比口径以补充证据",
        "plan": {"tasks": [{
            "id": "new_evidence", "task_goal": new_goal,
            "tool_search_query": new_query,
            "completion_criteria": ["提供可核实的财务指标"],
        }]},
    })
    result = review(client, current=current, results=[
        TaskResultModel(task_id="completed", status="success", content="已取得2024年指标"),
    ])
    assert result.plan.tasks[0].task_goal == new_goal
    assert len(client.calls) == 1


def test_revision_rejects_new_id_that_repeats_completed_research_goal():
    current = PlanModel(
        tasks=[
            {
                "id": "a_share_tech_positioning",
                "task_goal": (
                    "获取A股科技板块（半导体、光模块/算力硬件、消费电子等）当前的关键定位数据："
                    "板块指数相对近期高点的回撤幅度、估值分位（PE/PB历史分位）、最新两融余额变化方向、"
                    "北向资金/陆股通近期净流入流出情况，以及科技板块三季报业绩预告或景气度线索。"
                    "用于判断科技股是否具备继续繁荣的估值与资金基础。"
                ),
                "completion_criteria": [
                    "给出科技板块指数相对近期高点的回撤幅度",
                    "给出两融、北向资金与估值分位",
                ],
                "tool_search_query": "A股科技板块回撤估值两融资金数据",
            }
        ]
    )
    repeated = {
        "action": "revise",
        "reason": "补齐位置数据",
        "plan": {
            "tasks": [
                {
                    "id": "supplement_tech_position",
                    "task_goal": (
                        "补齐A股科技板块（半导体、算力硬件/光模块、消费电子等）的资金面与位置数据："
                        "最新两融余额规模及变化方向、北向资金/陆股通近期净流入流出方向、"
                        "科技板块指数相对近期高点的回撤幅度，以及可核实的估值分位（明确指数口径与PE/PB指标）。"
                        "用于判断科技股是否具备继续繁荣的资金与估值基础。"
                    ),
                    "completion_criteria": [
                        "给出科技板块指数相对近期高点的回撤幅度",
                        "给出两融、北向资金与估值分位",
                    ],
                    "tool_search_query": "A股科技板块回撤估值两融资金数据",
                }
            ]
        },
    }
    # 保留“仅更换 ID 不能重跑相同目标”的契约；不再把相近描述硬判语义等价。
    repeated["plan"]["tasks"][0]["task_goal"] = current.tasks[0].task_goal
    client = Decisions(repeated, repeated)

    with pytest.raises(PlannerOutputError):
        review(
            client,
            current=current,
            results=[
                TaskResultModel(
                    task_id="a_share_tech_positioning",
                    status="success",
                    content="已取得科技板块回撤、估值和资金数据",
                )
            ],
        )

    repair = json.loads(client.calls[1]["user_prompt"])["repair"]
    assert "重复" in repair["validation_error"]
    assert "a_share_tech_positioning" in repair["validation_error"]


@pytest.mark.parametrize("content",["[]","{}","null"])
def test_empty_execution_success_does_not_block_a_new_evidence_attempt(content):
    current=PlanModel(tasks=[{"id":"original","task_goal":"查询三个A股指数的历史收盘点位"}])
    client=Decisions({"action":"revise","reason":"原接口返回空，需选择能提供收盘点位的数据能力",
        "plan":{"tasks":[{"id":"alternative","task_goal":current.tasks[0].task_goal,
            "tool_search_query":"指数历史日线 收盘点位 YYYYMMDD"}]}})
    decision=review(client,current=current,results=[TaskResultModel(
        task_id="original",status="success",tool_name="mcp.data.wrong_interface",content=content,
    )])
    assert decision.action=="revise"
    assert len(client.calls)==1

def test_revision_allows_task_with_materially_new_evidence_gap():
    current = PlanModel(
        tasks=[
            {
                "id": "market_position",
                "task_goal": "获取A股科技板块相对近期高点的回撤幅度、估值分位和资金流向。",
                "completion_criteria": ["给出回撤幅度与估值分位"],
            }
        ]
    )
    decision = review(
        Decisions(
            {
                "action": "revise",
                "reason": "需要核实新增政策",
                "plan": {
                    "tasks": [
                        {
                            "id": "policy_catalyst",
                            "task_goal": "核实人工智能、半导体国产替代支持政策的落地时间和适用范围。",
                            "completion_criteria": ["列出政策文件、发布时间与适用对象"],
                        }
                    ]
                },
            }
        ),
        current=current,
        results=[
            TaskResultModel(
                task_id="market_position", status="success", content="已取得行情定位数据"
            )
        ],
    )

    assert decision.plan is not None
    assert decision.plan.tasks[0].id == "policy_catalyst"


def test_revision_budget_does_not_forbid_finish():
    assert review(Decisions({"action": "finish", "reason": "完成"}), remaining=0).action == "finish"
    with pytest.raises(PlannerOutputError):
        review(
            Decisions(
                {
                    "action": "revise",
                    "reason": "补查",
                    "plan": {"tasks": [{"id": "more", "task_goal": "补查"}]},
                }
            ),
            remaining=0,
        )


def test_feedback_context_includes_original_goal_plan_and_observations():
    client = Decisions({"action": "finish", "reason": "证据足够"})
    review(client)
    data = json.loads(client.calls[0]["user_prompt"])
    assert data["user_message"] == "只使用提供的资料分析原因"
    assert data["current_plan"]["tasks"][0]["id"] == "facts"
    assert data["observations"][0]["content"] == "已取得事实"
    assert data["required_schema"]["continue"]["action"] == "continue"
    assert "$defs" not in data["required_schema"]


def test_review_receives_typed_evidence_and_keeps_query_for_target_verification():
    client = Decisions({"action": "finish", "reason": "保留缺口"})
    review(client, results=[TaskResultModel(task_id="facts", status="success", content=json.dumps({
        "query": "实际执行的查询", "answer": "盘中事实", "results": [
            {"title": "交易所", "url": "https://example.com/source", "snippet": "", "published_at": None},
        ],
    }))])
    payload = json.loads(client.calls[0]["user_prompt"])
    assert isinstance(payload["observations"][0]["content"], dict)
    assert payload["observations"][0]["content"]["query"] == "实际执行的查询"
    assert len(payload["sources"]) == 1


def test_analysis_task_cannot_also_bind_skill():
    with pytest.raises(ValidationError):
        PlanModel(
            tasks=[
                {
                    "id": "bad",
                    "task_goal": "分析",
                    "execution_type": "analysis",
                    "skill_name": "skill",
                }
            ]
        )


def test_revision_keeps_completed_evidence_and_namespaces_only_new_tasks():
    state = create_initial_state(
        session_id="s", tenant_id="t", user_id="u", user_message="分析原因"
    )
    state.update(
        intent={
            "intent_type": "research",
            "domain": "equity",
            "subdomain": "company",
            "goal": "分析原因",
        },
        plan=plan().model_dump(),
        planner_iteration=1,
    )
    state["task_results"] = [{"task_id": "facts", "status": "success", "content": "已取得事实"}]
    revised = {
        "tasks": [
            plan().tasks[0].model_dump(),
            {
                "id": "verify",
                "task_goal": "进一步核实",
                "depends_on": ["facts"],
                "tool_search_query": "公告",
            },
        ]
    }
    result = build_planner_agent_node(
        llm_client=Decisions({"action": "revise", "reason": "缺少核实", "plan": revised})
    )(state)
    assert [t["id"] for t in result["plan"]["tasks"]] == ["facts", "r1_verify"]
    assert result["plan"]["tasks"][1]["depends_on"] == ["facts"]
    assert result["plan"]["tasks"][1]["tool_search_query"] == "公告"
    assert result["plan_history"][0]["plan"]["tasks"][1]["id"] == "analysis"
    assert result["planner_feedback_count"] == 1


def test_scheduler_requests_feedback_for_success_and_failure():
    from app.graph.runtime import _dispatch_after_scheduler

    for status in ("success", "business_failure", "terminal_failure"):
        state = create_initial_state(
            session_id="s", tenant_id="t", user_id="u", user_message="研究"
        )
        state.update(plan=plan().model_dump(), planner_iteration=1)
        state["task_results"] = [{"task_id": "facts", "status": status, "content": "观察"}]
        assert _dispatch_after_scheduler(state) == "planner_agent"


def test_scheduler_obeys_reviewed_continue_and_finish():
    from app.graph.planning.policy import observation_signature
    from app.graph.runtime import _dispatch_after_scheduler

    state = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="研究")
    current = plan()
    current.tasks[1] = current.tasks[1].model_copy(update={
        "execution_type": "tool", "task_goal": "根据事实进一步核实公告",
    })
    state.update(plan=current.model_dump(), planner_iteration=1)
    state["task_results"] = [{"task_id": "facts", "status": "success", "content": "观察"}]
    state["planner_reviewed_signature"] = observation_signature(current, state["task_results"])
    state["planner_decision"] = {"action": "continue", "reason": "继续分析"}
    sends = _dispatch_after_scheduler(state)
    assert sends[0].node == "tool_agent"
    state["planner_decision"] = {"action": "finish", "reason": "证据足够"}
    assert _dispatch_after_scheduler(state) == "synthesizer"


def test_feedback_budget_stop_is_runtime_limit_not_model_decision():
    from app.graph.planning.policy import MAX_PLANNER_FEEDBACKS
    from app.graph.runtime import task_scheduler_node

    state = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="研究")
    state.update(
        plan=plan().model_dump(), planner_iteration=1, planner_feedback_count=MAX_PLANNER_FEEDBACKS
    )
    state["task_results"] = [{"task_id": "facts", "status": "success", "content": "事实"}]
    update = task_scheduler_node(state)
    assert update["planner_stop_reason"] == "planner_feedback_budget_exhausted"
    assert "planner_decision" not in update


def test_invalid_feedback_stops_safely_without_claiming_a_model_decision():
    from app.graph.runtime import _dispatch_after_scheduler

    state = create_initial_state(
        session_id="s", tenant_id="t", user_id="u", user_message="分析原因"
    )
    state.update(
        intent={
            "intent_type": "research",
            "domain": "equity",
            "subdomain": "company",
            "goal": "分析原因",
        },
        plan=plan().model_dump(),
        planner_iteration=1,
    )
    state["task_results"] = [{"task_id": "facts", "status": "success", "content": "已取得部分事实"}]
    client = InvalidFeedback()
    result = build_planner_agent_node(llm_client=client)(state)
    assert len(client.calls) == 2
    assert result["planner_stop_reason"] == "planner_feedback_invalid"
    assert not result.get("planner_decision")
    assert _dispatch_after_scheduler(result) == "synthesizer"


def test_resume_policy_rejects_old_checkpoint():
    from app.graph.runtime import LegacyCheckpointIncompatibleError, _validate_planner_checkpoint

    with pytest.raises(LegacyCheckpointIncompatibleError):
        _validate_planner_checkpoint({"plan": plan().model_dump()})
    _validate_planner_checkpoint({"planner_policy_version": "model-feedback-v1"})


def test_trace_records_feedback_action_without_private_reason():
    from app.harness.tracing import summarize_node_transition

    state = create_initial_state(session_id="s", tenant_id="t", user_id="u", user_message="研究")
    state.update(
        plan=plan().model_dump(),
        planner_iteration=1,
        planner_feedback_count=1,
        planner_decision={"action": "finish", "reason": "私有业务细节"},
    )
    summary = summarize_node_transition(
        node_name="planner_agent", input_state=state, output_state=state
    )
    assert summary["planner_decision"]["action"] == "finish"
    assert summary["planner_decision"]["policy_version"] == "model-feedback-v1"
    assert "私有业务细节" not in json.dumps(summary, ensure_ascii=False)


def test_revision_can_reference_archived_success_without_reexecuting_it():
    client = Decisions(
        {
            "action": "revise",
            "reason": "使用旧证据",
            "plan": {
                "tasks": [
                    {
                        "id": "new_verification",
                        "task_goal": "依据历史信息核对新公告",
                        "execution_type": "tool",
                        "evidence_task_ids": ["archived"],
                    }
                ]
            },
        }
    )
    result = review(
        client, results=[TaskResultModel(task_id="archived", status="success", content="旧事实")]
    )
    assert result.plan.tasks[0].evidence_task_ids == ["archived"]


def test_revision_rejects_unavailable_evidence_reference():
    client = Decisions(
        {
            "action": "revise",
            "reason": "引用不存在证据",
            "plan": {
                "tasks": [
                    {"id": "new_analysis", "task_goal": "比较", "evidence_task_ids": ["missing"]}
                ]
            },
        }
    )
    with pytest.raises(PlannerOutputError):
        review(client)


def test_synthesis_receives_model_assessment_and_unresolved_questions():
    from app.graph.nodes.synthesizer import build_synthesizer_node

    state = create_initial_state(
        session_id="s", tenant_id="t", user_id="u", user_message="分析全年变化"
    )
    state.update(
        intent={"intent_type": "research", "goal": "分析全年变化"},
        plan=plan().model_dump(),
        planner_decision={
            "action": "finish",
            "reason": "只能确定单季度情况",
            "unresolved_questions": ["尚无全年可比数据"],
        },
    )
    state["task_results"] = [
        {"task_id": t.id, "status": "success", "content": "单季数据"} for t in plan().tasks
    ]
    client = Decisions({"answer": "材料不足"})
    build_synthesizer_node(llm_client=client)(state)
    payload = json.loads(client.calls[0]["user_prompt"])
    assert "尚无全年可比数据" in payload["data_limitations"]
    assert payload["research_assessment"]["reason"] == "只能确定单季度情况"
    assert state["draft_answer"]["confidence"] < 1.0


def test_revision_namespace_avoids_completed_id_collisions():
    from app.graph.nodes.planner_agent import _namespace_replanned_tasks

    original = PlanModel(
        tasks=[
            {"id": "r1_x", "task_goal": "旧任务"},
            {"id": "x", "task_goal": "新任务", "depends_on": ["r1_x"]},
        ]
    )
    revised = _namespace_replanned_tasks(original, revision=1, preserved_ids={"r1_x"})
    assert revised.tasks[0].id == "r1_x"
    assert revised.tasks[1].id != "r1_x"
    assert revised.tasks[1].depends_on == ["r1_x"]
