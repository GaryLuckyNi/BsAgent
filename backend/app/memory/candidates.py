import re
from hashlib import sha256

from app.memory.sensitivity import detect_sensitivity
from app.schemas.memory import MemoryCandidate
from app.tools.redaction import contains_inline_credentials


def propose_explicit_preference_candidates(
    *, user_message: str, tenant_id: str, user_id: str
) -> list[MemoryCandidate]:
    candidates = []
    for statement in _explicit_preference_statements(user_message):
        content = _extract_preference_content(statement)
        memory_key = _preference_key(statement, content)
        if not content or memory_key is None or contains_inline_credentials(content):
            continue
        sensitivity = detect_sensitivity(content)
        candidates.append(
            MemoryCandidate(
                candidate_id=_candidate_id(
                    tenant_id=tenant_id,
                    user_id=user_id,
                    memory_key=memory_key,
                    content=content,
                ),
                memory_type="user_preference",
                memory_key=memory_key,
                content=content,
                confidence=0.9,
                approval_status="pending" if sensitivity in {"medium", "high"} else "not_required",
                sensitivity=sensitivity,
                source="user_explicit_preference",
            )
        )
    return _deduplicate_candidates(candidates)


def _explicit_preference_statements(user_message: str) -> list[str]:
    statements = [
        statement.strip(" 。,，")
        for statement in re.split(r"[\n；;]+", user_message)
        if statement.strip()
    ]
    return [
        statement
        for statement in statements
        if any(marker in statement for marker in ("记住", "偏好", "我喜欢", "以后回答"))
    ]


def _extract_preference_content(statement: str) -> str:
    for separator in ("：", ":"):
        if separator in statement:
            return statement.split(separator, 1)[1].strip(" 。,，")
    for marker in ("请记住", "记住", "我的偏好是", "我喜欢"):
        if marker in statement:
            return statement.split(marker, 1)[1].strip(" 。,，")
    return statement.strip(" 。,，")


def _preference_key(statement: str, content: str) -> str | None:
    combined = f"{statement} {content}".lower()
    if any(term in combined for term in ("语言", "中文", "英文", "chinese", "english")):
        return "language"
    if any(term in combined for term in ("简洁", "简短", "详细", "展开", "回答偏好", "concise", "detail")):
        return "response_detail"
    if any(term in combined for term in ("关注", "行业", "领域", "focus domain")):
        return "focus_domain"
    if any(term in combined for term in ("邮箱", "邮件", "电话", "联系")):
        return "contact"
    return None


def _candidate_id(*, tenant_id: str, user_id: str, memory_key: str, content: str) -> str:
    fingerprint = sha256(
        f"{memory_key}:{_normalize_candidate_content(content)}".encode("utf-8")
    ).hexdigest()[:16]
    return f"memcand_{tenant_id}_{user_id}_user_preference_{fingerprint}"


def _deduplicate_candidates(candidates: list[MemoryCandidate]) -> list[MemoryCandidate]:
    merged: dict[tuple[str, str], MemoryCandidate] = {}
    for candidate in candidates:
        key = (candidate.memory_key or "", _normalize_candidate_content(candidate.content))
        existing = merged.get(key)
        if existing is None or _sensitivity_rank(candidate.sensitivity) > _sensitivity_rank(
            existing.sensitivity
        ):
            merged[key] = candidate
    return list(merged.values())


def _normalize_candidate_content(content: str) -> str:
    return re.sub(r"\s+", "", content.strip().lower())


def _sensitivity_rank(sensitivity: str) -> int:
    return {"low": 1, "medium": 2, "high": 3}[sensitivity]
