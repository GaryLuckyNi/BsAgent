import json

from app.graph.nodes.analysis_agent import build_analysis_agent_node
from app.graph.nodes.skill_agent import build_skill_agent_node
from app.graph.state import create_initial_state
from app.llm.schemas import LlmCompletionResult
from app.skills.definitions import SkillDefinition, SkillExecutionGuide


class FakeLlmClient:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[dict] = []

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        return LlmCompletionResult(
            content=self.content,
            model="fake",
            provider="fake",
        )


class FakeSkillRegistry:
    def get_definition_layers(self, *, name: str):
        assert name == "company_quality"
        return (
            SkillDefinition(
                name=name,
                version="1.0.0",
                description="评估公司质量",
                constraints=["不得补造数据"],
                definition_hash="definition-hash",
            ),
            SkillExecutionGuide(
                name=name,
                version="1.0.0",
                instructions=["比较盈利质量与现金流"],
                definition_hash="guide-hash",
            ),
        )


def _state():
    state = create_initial_state(
        session_id="session-1",
        tenant_id="tenant-1",
        user_id="user-1",
        user_message="分析海能达",
    )
    state["task_results"] = [
        {
            "task_id": "financial",
            "status": "success",
            "content": '{"revenue":100}',
            "error_code": None,
        }
    ]
    return state


def test_skill_worker_freezes_summary_then_aggregates_validated_results():
    llm = FakeLlmClient(
        json.dumps(
            {
                "steps": [{"instruction": "评估公司质量", "source": "评估公司质量"}],
                "output_requirement": "",
            }
        )
    )
    state = _state()
    state["active_task"] = {
        "id": "quality",
        "task_goal": "评估盈利质量",
        "depends_on": ["financial"],
        "skill_name": "company_quality",
    }
    node = build_skill_agent_node(registry=FakeSkillRegistry(), llm_client=llm)
    update = node(state)
    assert "task_results" not in update
    workflow = update["skill_workflows"]["quality"]
    assert workflow["summary"] == "评估公司质量"
    assert workflow["definition_hash"] == "definition-hash"
    child_id = workflow["steps"][0]["task_id"]
    workflow["checked_steps"] = [child_id]
    state["skill_workflows"] = update["skill_workflows"]
    state["task_results"].append({"task_id": child_id, "status": "success", "content": "盈利稳定"})
    llm.content = "盈利稳定，但现金流仍需核验。"
    completed = node(state)
    assert completed["task_results"][0]["status"] == "success"
    assert completed["skill_workflows"]["quality"]["status"] == "completed"
    prompt = json.loads(llm.calls[-1]["user_prompt"])
    assert prompt["dependencies"][0]["task_id"] == "financial"
    assert prompt["steps"][0]["task_id"] == child_id


def test_analysis_worker_reasons_only_from_existing_context():
    llm = FakeLlmClient("收入数据已取得，但尚不能判断持续性。")
    state = _state()
    state["active_task"] = {
        "id": "judge",
        "task_goal": "判断收入质量",
        "depends_on": ["financial"],
        "skill_name": None,
    }

    update = build_analysis_agent_node(llm_client=llm)(state)

    assert update["task_results"][0]["task_id"] == "judge"
    assert update["task_results"][0]["status"] == "success"
    assert "持续性" in update["task_results"][0]["content"]
    prompt = json.loads(llm.calls[0]["user_prompt"])
    assert prompt["task_goal"] == "判断收入质量"
    assert prompt["dependencies"][0]["content"] == '{"revenue":100}'


def test_analysis_worker_recovers_when_thinking_uses_budget_without_content():
    class EmptyThenContentLlm:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def complete(self, **kwargs):
            self.calls.append(kwargs)
            content = "" if len(self.calls) == 1 else "净息差通过利息净收入影响银行利润。"
            return LlmCompletionResult(
                content=content,
                model="fake",
                provider="fake",
                finish_reason="length" if not content else "stop",
            )

    llm = EmptyThenContentLlm()
    state = _state()
    state["active_task"] = {
        "id": "explain_nim",
        "task_goal": "解释净息差影响盈利的机制",
        "depends_on": [],
        "skill_name": None,
    }

    update = build_analysis_agent_node(llm_client=llm)(state)

    assert update["task_results"][0]["status"] == "success"
    assert "利息净收入" in update["task_results"][0]["content"]
    assert [call["thinking"] for call in llm.calls] == [True, False]


def test_analysis_sees_completion_conditions_history_and_image_materials():
    from langchain_core.messages import HumanMessage

    llm = FakeLlmClient("依据旧证据分析。")
    state = _state()
    state["messages"] = [HumanMessage(content="此前提供的财报材料")]
    state["image_analyses"] = [{"summary": "图片披露了年度收入"}]
    state["active_task"] = {
        "id": "judge",
        "task_goal": "分析",
        "execution_type": "analysis",
        "completion_criteria": ["按年度口径"],
        "evidence_task_ids": ["financial"],
    }
    build_analysis_agent_node(llm_client=llm)(state)
    payload = json.loads(llm.calls[0]["user_prompt"])
    assert payload["completion_criteria"] == ["按年度口径"]
    assert payload["dependencies"][0]["task_id"] == "financial"
    assert "图片披露了年度收入" in json.dumps(payload, ensure_ascii=False)
    assert "此前提供的财报材料" in json.dumps(payload, ensure_ascii=False)
