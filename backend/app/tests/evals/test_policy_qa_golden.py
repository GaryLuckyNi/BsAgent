from app.graph.runtime import run_question_answer_graph


def test_policy_qa_golden_case_cites_evidence_and_answers():
    state = run_question_answer_graph(
        session_id="golden_s1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="差旅报销需要什么？",
    )

    response = state["final_response"]
    verification = state["verification"]

    assert "差旅报销需要提交发票和审批单" in response["content"]
    assert response["cited_evidence_ids"] == ["ev_1"]
    assert verification["faithfulness_score"] >= 0.9
