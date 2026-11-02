"""恢复图保留公开进度与后续中断，不额外调用模型。"""

import pytest
from app.graph import runtime
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt


@pytest.mark.parametrize("pause_again", [False, True])
@pytest.mark.parametrize("pinned_checkpoint", [False, True])
def test_resume_stream_forwards_custom_progress_and_preserves_interrupt(pause_again, pinned_checkpoint):
    def node(state):
        interrupt("首次确认")
        get_stream_writer()({"type": "progress", "message": "搜索摘要"})
        if pause_again:
            interrupt("后续确认")
        return {"done": True}

    builder = StateGraph(dict)
    builder.add_node("work", node)
    builder.add_edge(START, "work")
    builder.add_edge("work", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "resume-progress"}}
    graph.invoke({"request_id": "r"}, config)
    if pinned_checkpoint:
        config = graph.get_state(config).config
    events = list(runtime._stream_resumed_graph(graph, Command(resume="确认"), config))
    assert isinstance(events[0], runtime.AgentGraphStreamEvent)
    assert events[0].event == "custom"
    assert events[0].data == {"type": "progress", "message": "搜索摘要"}
    if pause_again:
        assert events[-1]["__interrupt__"][0].value == "后续确认"
    else:
        assert events[-1]["done"] is True
        assert not events[-1].get("__interrupt__")
