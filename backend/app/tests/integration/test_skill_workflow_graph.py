"""使用真实图、工具执行器和 checkpoint 验证固定 Skill 的审批恢复。"""

import json
from types import SimpleNamespace

from app.graph import runtime
from app.graph.state import create_initial_state
from app.intent.catalog import BusinessIntentCatalog
from app.llm.schemas import LlmCompletionResult
from app.schemas.tools import ToolCallResult
from app.skills.definitions import SkillDefinition, SkillExecutionGuide
from app.tools.base import Tool
from app.tools.registry import ToolRegistry
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import BaseModel


class Input(BaseModel):
    value: str


class RecordingTool(Tool):
    version = "1"
    input_model = Input

    def __init__(self, name, calls, *, risk="low", missing=False):
        self.name, self.calls, self.risk_level, self.missing = name, calls, risk, missing

    async def run(self, data):
        self.calls.append((self.name, data.value))
        return ToolCallResult(
            tool_call_id="provider-id",
            tool_name=self.name,
            status="success",
            result={} if self.missing else {"value": data.value + ":" + self.name},
            trace_id="trace",
        )


class Registry:
    calls = 0

    def get_definition_layers(self, *, name):
        self.calls += 1
        return SkillDefinition(
            name=name,
            version="1",
            description="调用 first，必须返回 value。调用 second，必须返回 value。调用 third，必须返回 value。",
            definition_hash="frozen",
        ), SkillExecutionGuide(name=name, version="1", definition_hash="frozen")


class Llm:
    def __init__(self):
        self.compiles = 0
        self.bindings = []
        self.prompts = []

    async def complete(self, **kwargs):
        self.prompts.append(kwargs)
        if "工作流编译器" in kwargs["system_prompt"]:
            self.compiles += 1
            content = json.dumps(
                {
                    "steps": [
                        {
                            "instruction": f"调用 {name}",
                            "source": f"调用 {name}，必须返回 value。",
                            "tool_name": name,
                            "required_fields": ["value"],
                            "completion_criteria": [],
                        }
                        for name in ("first", "second", "third")
                    ],
                    "output_requirement": "",
                }
            )
        else:
            content = "三个步骤已完成，依据实际结果汇总。"
        return LlmCompletionResult(content=content, model="fake", provider="fake")

    def bind_tools(self, schemas, *, tool_choice):
        assert len(schemas) == 1
        assert tool_choice == "required"
        name = schemas[0]["function"]["name"]
        self.bindings.append(name)

        def invoke(messages):
            instruction = json.loads(messages[-2].content)
            dependencies = instruction["dependencies"]
            value = json.loads(dependencies[-1]["content"])["value"] if dependencies else "initial"
            return AIMessage(
                content="", tool_calls=[{"id": "untrusted", "name": name, "args": {"value": value}}]
            )

        return SimpleNamespace(invoke=invoke)


def setup_graph(monkeypatch, *, missing=False, second_risk="low", unavailable_first=False):
    calls, llm, skills = [], Llm(), Registry()
    tools = ToolRegistry()
    for name in ("first", "second", "third"):
        if unavailable_first and name == "first":
            continue
        tools.register(
            RecordingTool(
                name,
                calls,
                risk="high" if name == "first" else second_risk if name == "second" else "low",
                missing=missing and name == "first",
            )
        )
    reviews = []

    def planner(state, runtime=None):
        if state["plan"]:
            reviews.append(list(state["task_results"]))
            return {"planner_stop_reason": "test_complete"}
        return {
            "plan": {
                "tasks": [{"id": "workflow", "task_goal": "执行三个步骤", "skill_name": "example"}]
            },
            "planner_iteration": 1,
        }

    monkeypatch.setattr(runtime, "build_default_skill_registry", lambda: skills)
    monkeypatch.setattr(runtime, "build_planner_agent_node", lambda **kwargs: planner)
    monkeypatch.setattr(
        runtime,
        "build_intent_router_node",
        lambda **kwargs: lambda state: {"intent": {"intent_type": "financial_research"}},
    )
    monkeypatch.setattr(
        runtime,
        "build_synthesizer_node",
        lambda **kwargs: (
            lambda state, runtime=None: {
                "draft_answer": {
                    "content": "完成或说明失败",
                    "claims": [],
                    "cited_evidence_ids": [],
                }
            }
        ),
    )
    monkeypatch.setattr(runtime, "verifier_node", lambda state: {})
    monkeypatch.setattr(runtime, "approval_coordinator_node", lambda state: {})
    graph = runtime.build_question_answer_graph(
        business_intent_catalog=BusinessIntentCatalog([]),
        llm_client=llm,
        tool_registry_factory=lambda: tools,
        checkpointer=InMemorySaver(),
    )
    state = create_initial_state(
        session_id="session",
        tenant_id="tenant",
        user_id="user",
        user_message="执行 Skill",
        tool_catalog_digest=tools.catalog_digest,
    )
    config = {"configurable": {"thread_id": "workflow-test"}, "recursion_limit": 128}
    return graph, state, config, calls, llm, skills, reviews


def decide(graph, config, decision="approved"):
    snapshot = graph.get_state(config)
    payload = snapshot.tasks[0].interrupts[0].value
    return graph.invoke(
        Command(
            resume={
                "approval_id": payload["approval_id"],
                "action_digest": payload["action_digest"],
                "decision": decision,
            }
        ),
        config,
    )


def test_three_steps_pause_before_first_tool_and_resume_same_arguments(monkeypatch):
    graph, state, config, calls, llm, skills, reviews = setup_graph(monkeypatch)
    paused = graph.invoke(state, config)
    assert paused["__interrupt__"]
    assert calls == []
    assert llm.bindings == ["first"]
    assert reviews == []
    result = decide(graph, config)
    assert [name for name, _ in calls] == ["first", "second", "third"]
    assert calls[1][1] == "initial:first"
    assert calls[2][1] == "initial:first:second"
    assert llm.bindings == ["first", "second", "third"]
    assert llm.compiles == skills.calls == 1
    assert result["skill_workflows"]["workflow"]["status"] == "completed"
    assert len(reviews) == 1
    assert len(reviews[0]) == 4
    graph.invoke(None, config)
    assert len(calls) == 3


def test_rejection_never_executes_first_or_dependent_tools(monkeypatch):
    graph, state, config, calls, _llm, _skills, reviews = setup_graph(monkeypatch)
    graph.invoke(state, config)
    result = decide(graph, config, "rejected")
    assert calls == []
    assert result["approval_status"].status == "rejected"
    assert reviews == []


def test_missing_required_output_stops_workflow_without_replanning(monkeypatch):
    graph, state, config, calls, _llm, _skills, reviews = setup_graph(monkeypatch, missing=True)
    graph.invoke(state, config)
    result = decide(graph, config)
    assert [name for name, _ in calls] == ["first"]
    assert result["skill_workflows"]["workflow"]["status"] == "failed"
    assert reviews == []
    assert result["task_results"][-1]["error_code"] == "skill_step_incomplete"


def test_second_approval_does_not_reexecute_first_step(monkeypatch):
    graph, state, config, calls, llm, _skills, _reviews = setup_graph(
        monkeypatch, second_risk="high"
    )
    graph.invoke(state, config)
    paused = decide(graph, config)
    assert paused["__interrupt__"]
    assert [name for name, _ in calls] == ["first"]
    result = decide(graph, config)
    assert [name for name, _ in calls] == ["first", "second", "third"]
    assert llm.bindings == ["first", "second", "third"]
    assert result["skill_workflows"]["workflow"]["status"] == "completed"


def test_context_routing_executes_compiled_skill_steps_before_synthesis(monkeypatch):
    graph, state, config, calls, llm, _skills, reviews = setup_graph(monkeypatch)
    state["request_flow_version"] = "context-routing-v1"

    paused = graph.invoke(state, config)
    assert paused["__interrupt__"]
    assert paused["skill_workflows"]["workflow"]["status"] == "running"
    assert paused["draft_answer"] is None
    assert calls == []

    result = decide(graph, config)
    assert [name for name, _ in calls] == ["first", "second", "third"]
    assert llm.compiles == 1
    assert result["skill_workflows"]["workflow"]["status"] == "completed"
    assert result["draft_answer"] is not None
    assert reviews == []


def test_context_routing_missing_skill_tool_never_creates_executable_steps(monkeypatch):
    graph, state, config, calls, llm, _skills, reviews = setup_graph(
        monkeypatch, unavailable_first=True
    )
    state["request_flow_version"] = "context-routing-v1"

    result = graph.invoke(state, config)

    # 模型返回 steps 不代表编译成功：绑定失败不能发布部分工作流或偷偷换工具。
    assert llm.compiles == 1
    assert result["skill_workflows"] == {}
    assert result["task_results"][0]["error_code"] == "skill_tool_not_found"
    assert result["task_results"][0]["status"] == "terminal_failure"
    assert result["draft_answer"] is not None
    assert calls == []
    assert reviews == []
