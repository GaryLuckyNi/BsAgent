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


def test_skill_worker_uses_full_definition_and_dependency_results():
    llm = FakeLlmClient("盈利稳定，但现金流仍需核验。")
    state = _state()
    state["active_task"] = {
        "id": "quality",
        "task_goal": "评估盈利质量",
        "depends_on": ["financial"],
        "skill_name": "company_quality",
        "tool_name": None,
    }

    update = build_skill_agent_node(
        registry=FakeSkillRegistry(),
        llm_client=llm,
    )(state)

    assert update["task_results"][0] == {
        "task_id": "quality",
        "status": "success",
        "content": "盈利稳定，但现金流仍需核验。",
        "error_code": None,
    }
    prompt = json.loads(llm.calls[0]["user_prompt"])
    assert prompt["instructions"] == ["比较盈利质量与现金流"]
    assert prompt["dependencies"][0]["task_id"] == "financial"


def test_analysis_worker_reasons_only_from_existing_context():
    llm = FakeLlmClient("收入数据已取得，但尚不能判断持续性。")
    state = _state()
    state["active_task"] = {
        "id": "judge",
        "task_goal": "判断收入质量",
        "depends_on": ["financial"],
        "skill_name": None,
        "tool_name": None,
    }

    update = build_analysis_agent_node(llm_client=llm)(state)

    assert update["task_results"][0]["task_id"] == "judge"
    assert update["task_results"][0]["status"] == "success"
    assert "持续性" in update["task_results"][0]["content"]
    prompt = json.loads(llm.calls[0]["user_prompt"])
    assert prompt["task_goal"] == "判断收入质量"
    assert prompt["dependencies"][0]["content"] == '{"revenue":100}'
