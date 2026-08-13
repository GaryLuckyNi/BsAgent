from datetime import UTC, datetime

from app.memory.retriever import ExperienceMemoryRetriever


class ExperienceEntry:
    def __init__(
        self,
        *,
        source_candidate_id: str,
        memory_type: str,
        content: str,
        source: str | None = None,
    ) -> None:
        self.entry_id = f"exp_{source_candidate_id}"
        self.tenant_id = "tenant_1"
        self.user_id = "user_1"
        self.memory_type = memory_type
        self.content = content
        self.source_candidate_id = source_candidate_id
        self.sensitivity = "low"
        self.source = source
        self.created_at = datetime(2026, 6, 28, tzinfo=UTC)


class ExperienceStore:
    def __init__(self) -> None:
        self.query = None
        self.limit = None

    def list_for_user(self, *, tenant_id: str, user_id: str, query=None, limit=None):
        assert tenant_id == "tenant_1"
        assert user_id == "user_1"
        self.query = query
        self.limit = limit
        return [
            ExperienceEntry(
                source_candidate_id="memcand_1",
                memory_type="user_preference",
                content="所有回答用中文",
                source="user_explicit_preference",
            )
        ]


def test_experience_memory_retriever_maps_entries_to_evidence_package():
    store = ExperienceStore()
    package = ExperienceMemoryRetriever(store, recall_limit=6)(
        "差旅报销需要什么？",
        tenant_id="tenant_1",
        user_id="user_1",
    )

    assert package.evidence_package_id == "evp_experience_memory"
    assert package.coverage == "sufficient"
    assert package.items[0].chunk_id == "experience/memcand_1"
    assert package.items[0].content == "用户偏好：所有回答用中文"
    assert package.items[0].metadata["memory_type"] == "user_preference"
    assert store.query == "差旅报销需要什么？"
    assert store.limit == 6
