import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from langgraph.runtime import Runtime
from langgraph.store.base import Item

from app.core.config import settings
from app.graph.state import AgentState


logger = logging.getLogger(__name__)
_PREFERENCE_MEMORY_TYPE = "user_preference"


@dataclass(frozen=True)
class UserPreference:
    memory_key: str
    content: str


def experience_namespace(tenant_id: str, user_id: str) -> tuple[str, ...]:
    return ("tenants", tenant_id, "users", user_id, "experiences")


def decode_user_preference(item: Item) -> UserPreference | None:
    value = item.value
    if not isinstance(value, Mapping):
        return None
    if value.get("memory_type") != _PREFERENCE_MEMORY_TYPE:
        return None
    content = value.get("content")
    memory_key = value.get("memory_key", item.key)
    if not isinstance(content, str) or not content.strip():
        return None
    if not isinstance(memory_key, str) or not memory_key.strip():
        return None
    return UserPreference(memory_key=memory_key, content=content.strip())


def recall_user_preferences(
    runtime: Runtime | None,
    state: AgentState,
    *,
    on_failure: Callable[[AgentState], None] | None = None,
) -> list[UserPreference]:
    store = runtime.store if runtime is not None else None
    if store is None:
        _record_recall_failure(state, on_failure=on_failure, reason="store_unavailable")
        return []
    try:
        items = store.search(
            experience_namespace(state["tenant_id"], state["user_id"]),
            query=state["user_message"],
            filter={"memory_type": _PREFERENCE_MEMORY_TYPE},
            limit=settings.experience_memory_recall_limit,
        )
        preferences = []
        for item in items:
            preference = decode_user_preference(item)
            if preference is not None:
                preferences.append(preference)
        return preferences
    except Exception:
        _record_recall_failure(state, on_failure=on_failure, reason="store_read_failed")
        return []


def planning_preference_constraints(
    preferences: list[UserPreference],
) -> dict[str, str]:
    constraints: dict[str, str] = {}
    for preference in preferences:
        content = preference.content.lower()
        if preference.memory_key == "language":
            if "英文" in content or "english" in content:
                constraints["language"] = "english"
            elif "中文" in content or "chinese" in content:
                constraints["language"] = "chinese"
        elif preference.memory_key == "response_detail":
            if any(term in content for term in ("简洁", "简短", "精简", "concise")):
                constraints["detail"] = "concise"
            elif any(term in content for term in ("详细", "展开", "detail")):
                constraints["detail"] = "detailed"
        elif preference.memory_key == "focus_domain":
            constraints["focus_domain"] = preference.content
    return constraints


def presentation_preference_constraints(
    preferences: list[UserPreference],
) -> dict[str, str]:
    constraints = planning_preference_constraints(preferences)
    return {
        key: value
        for key, value in constraints.items()
        if key in {"language", "detail"}
    }


def _record_recall_failure(
    state: AgentState,
    *,
    on_failure: Callable[[AgentState], None] | None,
    reason: str,
) -> None:
    logger.warning(
        "experience memory recall failed",
        extra={
            "event": "experience_memory_recall_failed",
            "reason": reason,
            "tenant_id": state["tenant_id"],
            "user_id": state["user_id"],
            "trace_id": state["trace_id"],
        },
    )
    if on_failure is not None:
        on_failure(state)
