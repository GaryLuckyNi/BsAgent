from app.graph.nodes.planner_agent import build_planner_agent_node
from app.graph.planning.equity_research import build_equity_research_evidence_plan
from app.graph.planning.templates import build_direct_response_plan
from app.graph.planning.tool_action import build_tool_action_plan


def test_planner_capabilities_have_independent_module_boundaries():
    assert callable(build_planner_agent_node)
    assert callable(build_equity_research_evidence_plan)
    assert callable(build_direct_response_plan)
    assert callable(build_tool_action_plan)
