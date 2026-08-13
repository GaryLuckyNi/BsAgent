from typing import Protocol

from app.schemas.evidence import EvidenceItem, EvidencePackage


class ExperienceEntryLike(Protocol):
    source_candidate_id: str
    user_id: str
    memory_type: str
    content: str
    source: str | None


class ExperienceReader(Protocol):
    def list_for_user(
        self,
        *,
        tenant_id: str,
        user_id: str,
        query: str | None = None,
        limit: int | None = None,
    ) -> list[ExperienceEntryLike]:
        raise NotImplementedError


class ExperienceMemoryRetriever:
    def __init__(self, experience_reader: ExperienceReader, *, recall_limit: int = 8) -> None:
        self._experience_reader = experience_reader
        self._recall_limit = recall_limit

    def __call__(self, query: str, *, tenant_id: str, user_id: str) -> EvidencePackage:
        entries = self._experience_reader.list_for_user(
            tenant_id=tenant_id,
            user_id=user_id,
            query=query,
            limit=self._recall_limit,
        )
        return EvidencePackage(
            evidence_package_id="evp_experience_memory",
            query_set=[query],
            coverage="sufficient" if entries else "none",
            items=[
                EvidenceItem(
                    evidence_id=f"memory_{entry.source_candidate_id}",
                    chunk_id=f"experience/{entry.source_candidate_id}",
                    document_id=f"experience/{entry.user_id}",
                    title=_title_for_memory_type(entry.memory_type),
                    content=f"{_title_for_memory_type(entry.memory_type)}：{entry.content}",
                    score=1.0,
                    metadata={
                        "memory_type": entry.memory_type,
                        "source": entry.source,
                    },
                )
                for entry in entries
            ],
        )


def _title_for_memory_type(memory_type: str) -> str:
    if memory_type == "user_preference":
        return "用户偏好"
    return "长期经验"
