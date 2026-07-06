from app.graph.state import AgentState


def _successful_stock_tool_results(state: AgentState) -> list[dict]:
    results = []
    for call in state["tool_calls"]:
        if not call.request.tool_name.startswith("mcp.mock.stock."):
            continue
        if call.result is None or call.result.status != "success":
            continue
        results.append(
            {
                "tool_name": call.request.tool_name,
                "purpose": call.request.purpose,
                "result": call.result.result or {},
            }
        )
    return results


def analysis_agent_node(state: AgentState) -> AgentState:
    evidence = state["evidence"]
    structured_tool_results = _successful_stock_tool_results(state)
    if evidence is None or not evidence.items:
        state["analysis"] = {
            "findings": [],
            "structured_tool_results": structured_tool_results,
            "missing_information": [] if structured_tool_results else ["没有可用证据"],
            "confidence": 0.6 if structured_tool_results else 0.0,
        }
        return state

    state["analysis"] = {
        "findings": [item.content for item in evidence.items],
        "structured_tool_results": structured_tool_results,
        "missing_information": [],
        "confidence": max(item.score for item in evidence.items),
    }
    return state
