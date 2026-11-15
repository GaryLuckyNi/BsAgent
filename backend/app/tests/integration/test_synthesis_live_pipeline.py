"""真实合成节点、HTTP适配、LangGraph及持久化订阅贯通；提供方仍在生成时展示正文。"""
import json
from threading import Event, Thread

import httpx
from app.conversations.durable import DurableRunService
from app.conversations.execution import produce_frames, subscribe_events
from app.conversations.service import BeginTurn, ConversationService
from app.graph import runtime
from app.graph.nodes.synthesizer import build_synthesizer_node
from app.graph.planning.answer_citations import AnswerCitations
from app.graph.state import AgentState, create_initial_state
from app.harness.node_runner import NodeRunner
from app.llm.deepseek import DeepSeekClient
from app.tests.integration.test_session_api import create_test_app_with_database
from app.tests.unit.test_protocol_replay_security import decoded_frames
from langgraph.graph import END, StateGraph


def test_synthesis_body_and_text_after_citation_arrive_before_model_finishes():
    _app, factory = create_test_app_with_database()
    with factory() as db:
        handle = ConversationService(db).begin_turn(BeginTurn(
            tenant_id="tenant_1", user_id="user_1", session_id="s1",
            request_id="live-synthesis-chain", user_content="分析公开财报",
        ))
        DurableRunService(db).prepare(handle=handle, payload={})
        db.commit()
    state = create_initial_state(session_id="s1", tenant_id="tenant_1", user_id="user_1", user_message="分析公开财报")
    url = "https://example.invalid/annual-report"
    state.update(request_flow_version="context-routing-v1", intent={"intent_type":"financial_research", "goal":"分析公开财报"},
        plan={"tasks":[{"id":"facts", "task_goal":"分析公开财报"}]},
        task_results=[{"task_id":"facts", "status":"success", "tool_name":"web_search",
            "content":json.dumps({"results":[{"url":url,"title":"公开报告","snippet":"公开财报数据"}]})}])
    first_body = "公开财报显示经营现金流保持稳定" * 5 + "（source_1）。" + "引用后的分析仍在持续生成且尚未结束" * 12
    second_body = "后半段继续补充新的分析" * 12
    text = first_body + second_body
    first_raw = '{"action":"answer"}\n' + first_body
    waiting, proceed, visible = Event(), Event(), Event()
    provider_finished = Event()
    second_phase_started = Event()
    observed, drafts, failures = [], [], []
    class Body(httpx.SyncByteStream):
        def __iter__(self):
            for start in range(0, len(first_raw), 12):
                packet = {"choices":[{"delta":{"content":first_raw[start:start+12]}}]}
                yield ("data: " + json.dumps(packet) + "\n\n").encode()
            waiting.set()
            assert proceed.wait(8)
            second_phase_started.set()
            for start in range(0, len(second_body), 12):
                packet = {"choices":[{"delta":{"content":second_body[start:start+12]}}]}
                yield ("data: " + json.dumps(packet) + "\n\n").encode()
            provider_finished.set()
            yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    with httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(200,stream=Body())), base_url="https://synthetic.invalid") as transport:
        client = DeepSeekClient(api_key="synthetic", model="test", sync_http_client=transport)
        synthesize = build_synthesizer_node(llm_client=client)
        def node(value):
            result = synthesize(value)
            drafts.append(result["draft_answer"])
            return result
        graph = StateGraph(AgentState)
        runtime._add_harnessed_node(graph, "synthesizer", node, NodeRunner())
        graph.set_entry_point("synthesizer")
        graph.add_edge("synthesizer", END)
        compiled = graph.compile()
        def frames(db):
            for event in compiled.stream(state, stream_mode="custom"):
                if event.get("type") == "answer_delta":
                    data = [{"id":handle.assistant_message_id,"type":"ai","content":event["content"]},{}]
                    yield "event: messages\ndata: " + json.dumps(data,ensure_ascii=False) + "\n\n"
            draft = drafts[-1]
            ConversationService(db).complete_turn(tenant_id="tenant_1",run_id=handle.run_id,content=draft["content"])
            db.commit()
            yield "event: custom\ndata: " + json.dumps({"type":"final","response":draft},ensure_ascii=False) + "\n\n"
        def consume():
            source = subscribe_events(session_factory=factory,run_id=handle.run_id)
            try:
                for frame in source:
                    if frame.startswith("event: messages"):
                        observed.append(decoded_frames([frame])[0][2][0]["content"])
                        if "引用后的分析" in "".join(observed):
                            assert not provider_finished.is_set()
                            assert not second_phase_started.is_set()
                            visible.set()
                            return
            except Exception as error:  # noqa: BLE001 - 将消费线程异常交给主测试断言。
                failures.append(error)
            finally:
                source.close()
        producer = Thread(target=produce_frames,kwargs={"session_factory":factory,"run_id":handle.run_id,"frames_factory":frames})
        consumer = Thread(target=consume)
        producer.start()
        try:
            assert waiting.wait(5)
            consumer.start()
            shown_before_finish = visible.wait(3)
        finally:
            proceed.set()
            producer.join(5)
            if consumer.ident is not None:
                consumer.join(5)
        assert not failures and shown_before_finish
        assert not producer.is_alive() and not consumer.is_alive()
    replay = decoded_frames(subscribe_events(session_factory=factory,run_id=handle.run_id))
    content = ''.join(data[0]['content'] for _,event,data in replay if event=='messages')
    expected = AnswerCitations([{"source_id":"source_1","url":url}]).render(text)
    assert content == expected
    assert '"action"' not in content and 'source_1' not in content
