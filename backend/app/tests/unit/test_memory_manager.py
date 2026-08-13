from app.graph.nodes.memory_manager import (
    MemoryManager,
    build_memory_manager_node,
)
from app.graph.state import create_initial_state
from app.schemas.evidence import EvidenceItem, EvidencePackage
from app.schemas.memory import MemoryCandidate, MemoryContext


def test_memory_manager_loads_context_from_retrieval_adapter():
    def retrieve(query: str, *, tenant_id: str, user_id: str) -> EvidencePackage:
        assert query == "我喜欢中文文档，报销制度是什么？"
        assert tenant_id == "tenant_1"
        assert user_id == "user_1"
        return EvidencePackage(
            evidence_package_id="evp_memory",
            query_set=[query],
            coverage="sufficient",
            items=[
                EvidenceItem(
                    evidence_id="mem_1",
                    chunk_id="memory/user_1#1",
                    document_id="memory/user_1",
                    title="用户偏好",
                    content="用户偏好：所有文档使用中文。",
                    score=0.92,
                    metadata={"memory_type": "user_preference"},
                )
            ],
        )

    context = MemoryManager(retrieve_memory=retrieve).load_context(
        query="我喜欢中文文档，报销制度是什么？",
        tenant_id="tenant_1",
        user_id="user_1",
    )

    assert context.entries == ["用户偏好：所有文档使用中文。"]
    assert context.sources == ["memory/user_1#1"]


def test_memory_manager_proposes_user_preference_candidate_from_explicit_statement():
    candidates = MemoryManager().propose_candidates(
        user_message="记住我的语言偏好，所有的文档用中文",
        tenant_id="tenant_1",
        user_id="user_1",
    )

    assert candidates == [
        MemoryCandidate(
            candidate_id="memcand_tenant_1_user_1_user_preference_37b0d342d6e6",
            memory_type="user_preference",
            memory_key="language",
            content="所有的文档用中文",
            confidence=0.9,
            approval_status="not_required",
            sensitivity="low",
            source="user_explicit_preference",
        )
    ]


def test_memory_manager_ignores_long_term_knowledge_for_memory_candidates():
    candidates = MemoryManager().propose_candidates(
        user_message="记住：公司报销上限是每人每天1000元",
        tenant_id="tenant_1",
        user_id="user_1",
    )

    assert candidates == []


def test_memory_manager_marks_sensitive_preference_as_pending_for_approval():
    candidates = MemoryManager().propose_candidates(
        user_message="记住我的联系偏好：发邮件到 alice@example.com",
        tenant_id="tenant_1",
        user_id="user_1",
    )

    assert len(candidates) == 1
    assert candidates[0].content == "发邮件到 alice@example.com"
    assert candidates[0].sensitivity == "high"
    assert candidates[0].approval_status == "pending"
    assert candidates[0].memory_key == "contact"


def test_memory_manager_assigns_stable_keys_to_supported_preferences():
    manager = MemoryManager()

    language = manager.propose_candidates(
        user_message="记住我的语言偏好：以后都用中文",
        tenant_id="tenant_1",
        user_id="user_1",
    )
    detail = manager.propose_candidates(
        user_message="记住我的回答偏好：回答简洁一些",
        tenant_id="tenant_1",
        user_id="user_1",
    )
    focus = manager.propose_candidates(
        user_message="记住我的关注偏好：重点关注半导体行业",
        tenant_id="tenant_1",
        user_id="user_1",
    )

    assert [language[0].memory_key, detail[0].memory_key, focus[0].memory_key] == [
        "language",
        "response_detail",
        "focus_domain",
    ]


def test_memory_manager_deduplicates_repeated_user_preference_candidates():
    candidates = MemoryManager().propose_candidates(
        user_message="记住我的语言偏好：所有文档用中文\n记住我的语言偏好：所有文档用中文",
        tenant_id="tenant_1",
        user_id="user_1",
    )

    assert len(candidates) == 1
    assert candidates[0].content == "所有文档用中文"


def test_memory_manager_node_attaches_context_and_candidates_to_state():
    state = create_initial_state(
        session_id="session_1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="记住我的语言偏好，所有的文档用中文",
    )
    node = build_memory_manager_node(manager=MemoryManager())

    result = node(state)

    assert isinstance(result["memory_context"], MemoryContext)
    assert result["memory_candidates"][0].memory_type == "user_preference"


def test_memory_manager_node_creates_approval_for_sensitive_memory_candidate():
    state = create_initial_state(
        session_id="session_1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="记住我的联系偏好：发邮件到 alice@example.com",
    )
    node = build_memory_manager_node(manager=MemoryManager())

    result = node(state)

    assert len(result["approval_requests"]) == 1
    assert result["approval_requests"][0].risk_level == "high"
    assert result["approval_requests"][0].payload["content"] == "发邮件到 alice@example.com"


def test_memory_manager_node_does_not_create_memory_approval_for_knowledge():
    state = create_initial_state(
        session_id="session_1",
        tenant_id="tenant_1",
        user_id="user_1",
        user_message="记住：公司报销上限是每人每天1000元",
    )
    node = build_memory_manager_node(manager=MemoryManager())

    result = node(state)

    assert result["memory_candidates"] == []
    assert result["approval_requests"] == []
