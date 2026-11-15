import re

from app.memory.sensitivity import detect_sensitivity
from app.schemas.memory import MemoryCandidate
from app.security.context_content import context_content_is_safe
from app.security.memory_candidate import candidate_identity


def propose_explicit_preference_candidates(
    *, user_message: str, tenant_id: str, user_id: str
) -> list[MemoryCandidate]:
    candidates = []
    for statement in _explicit_preference_statements(user_message):
        content = _extract_preference_content(statement)
        memory_key = _preference_key(statement, content)
        if not content or memory_key is None or not context_content_is_safe(content):
            continue
        sensitivity = detect_sensitivity(content)
        candidate = MemoryCandidate(
            candidate_id="",
            memory_type="user_preference",
            memory_key=memory_key,
            content=content,
            confidence=0.9,
            approval_status="pending" if sensitivity in {"medium", "high"} else "not_required",
            sensitivity=sensitivity,
            source="user_explicit_preference",
        )
        candidate.candidate_id = candidate_identity(candidate, tenant_id=tenant_id, user_id=user_id)
        candidates.append(candidate)
    return _deduplicate_candidates(candidates)


def _explicit_preference_statements(user_message: str) -> list[str]:
    statements = [
        statement.strip(" 。,，")
        for statement in re.split(r"[\n；;]+", user_message)
        if statement.strip()
    ]
    return [statement for statement in statements if _is_explicit_preference_statement(statement)]


def _is_explicit_preference_statement(statement: str) -> bool:
    normalized = statement.strip()
    if "?" in normalized or "？" in normalized:
        return False
    if any(
        marker in normalized
        for marker in ("是什么", "可以记住什么", "能记住什么", "如何记住", "怎么记住")
    ):
        return False
    if normalized.startswith(("请记住", "记住", "以后请", "以后回答")):
        return True
    if normalized.startswith(("我喜欢", "我偏好", "我希望", "我更关注", "我关注")):
        return True
    return bool(re.match(r"^我的(?:回答)?偏好(?:是|为).+", normalized))


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
    if any(
        term in combined
        for term in ("简洁", "简短", "详细", "展开", "回答偏好", "concise", "detail")
    ):
        return "response_detail"
    if any(term in combined for term in ("关注", "行业", "领域", "focus domain")):
        return "focus_domain"
    if any(term in combined for term in ("邮箱", "邮件", "电话", "联系")):
        return "contact"
    return None


def _deduplicate_candidates(candidates: list[MemoryCandidate]) -> list[MemoryCandidate]:
    merged: dict[str, MemoryCandidate] = {}
    for candidate in candidates:
        merged.setdefault(candidate.candidate_id, candidate)
    return list(merged.values())
