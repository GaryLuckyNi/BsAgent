from datetime import UTC, datetime, timedelta

import pytest
from app.security.dependencies import get_current_principal
from app.security.principal import Principal
from app.storage.models import AgentMessage, AgentSession
from app.tests.integration.test_session_api import create_test_app_with_database
from fastapi.testclient import TestClient
from sqlalchemy import event


@pytest.fixture
def search_app():
    app, database = create_test_app_with_database()
    return TestClient(app), database, app


def seed_messages(database, contents, *, session_id="research", tenant="tenant_1",
                  user="user_1", deleted=False):
    session_key = f"{tenant}-{user}-{session_id}"
    with database() as db:
        db.add(AgentSession(
            session_key=session_key, session_id=session_id, tenant_id=tenant,
            user_id=user, status="deleted" if deleted else "active",
        ))
        for index, content in enumerate(contents):
            db.add(AgentMessage(
                message_id=f"{session_key}-{index:04}", session_key=session_key,
                tenant_id=tenant, user_id=user, run_id=f"run-{session_key}-{index}",
                request_id=f"request-{session_key}-{index}",
                role="user" if index % 2 == 0 else "assistant", status="completed",
                content=content, sequence_no=index + 1,
                created_at=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=index),
            ))
        db.commit()
    return session_key


def test_search_returns_each_matching_message_and_reaches_deep_history(search_app):
    client, database, _ = search_app
    key = seed_messages(database, ["最初标题", "old NEEDLE answer", "needle question"]
                        + ["后续无关消息"] * 120)
    response = client.get("/api/sessions/search", params={"q": "  Needle  "})
    assert response.status_code == 200
    assert response.json()["next_offset"] is None
    items = response.json()["items"]
    assert [item["message_id"] for item in items] == [f"{key}-0002", f"{key}-0001"]
    assert [item["role"] for item in items] == ["user", "assistant"]
    assert [item["sequence_no"] for item in items] == [3, 2]
    assert all(item["session_id"] == "research" and item["title"] == "最初标题"
               for item in items)
    assert all(item["created_at"].endswith("Z") for item in items)


def test_search_escapes_literal_wildcards_and_empty_query_is_empty(search_app):
    client, database, _ = search_app
    seed_messages(database, ["title", "actual 20%_AbC", "wrong 20xxABC"])
    response = client.get("/api/sessions/search", params={"q": "20%_aBc"})
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    assert "20%_AbC" in response.json()["items"][0]["snippet"]
    for query in ("", " \t\n "):
        assert client.get("/api/sessions/search", params={"q": query}).json() == {
            "items": [], "next_offset": None,
        }


def test_search_filters_deleted_messages_sessions_and_other_owners(search_app):
    client, database, _ = search_app
    key = seed_messages(database, ["deleted needle title", "needle visible", "needle title"])
    seed_messages(database, ["needle hidden"], session_id="deleted", deleted=True)
    seed_messages(database, ["needle other user"], user="user_2")
    seed_messages(database, ["needle other tenant"], tenant="tenant_2")
    with database() as db:
        db.get(AgentMessage, f"{key}-0000").deleted_at = datetime.now(UTC)
        db.commit()
    response = client.get("/api/sessions/search", params={"q": "needle"})
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 2
    assert all(item["title"] == "needle title" for item in items)
    assert {item["message_id"] for item in items} == {f"{key}-0001", f"{key}-0002"}


def test_search_pagination_uses_timestamp_and_message_id_without_gaps(search_app):
    client, database, _ = search_app
    keys = [seed_messages(database, ["needle"] * 3, session_id=name) for name in ("a", "b")]
    all_ids = [f"{key}-{index:04}" for index in range(3) for key in keys]
    all_ids.sort(key=lambda value: (value[-4:], value), reverse=True)
    actual = []
    offset = 0
    while offset is not None:
        response = client.get("/api/sessions/search", params={"q": "needle", "limit": 2,
                                                             "offset": offset})
        assert response.status_code == 200
        actual.extend(item["message_id"] for item in response.json()["items"])
        offset = response.json()["next_offset"]
    assert actual == all_ids


def test_search_snippet_centers_first_hit_and_removes_html_and_markdown(search_app):
    client, database, _ = search_app
    content = "<p>" + "前" * 100 + "**Needle**" + "后" * 300 + "</p>"
    seed_messages(database, ["标题" * 100, content])
    response = client.get("/api/sessions/search", params={"q": "needle"})
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["title"] == ("标题" * 100)[:160]
    assert item["snippet"] == "…" + "前" * 60 + "Needle" + "后" * 160 + "…"


def test_search_paginates_in_database_before_loading_content(search_app):
    client, database, _ = search_app
    seed_messages(database, ["needle"] * 12)
    statements = []
    engine = database.kw["bind"]

    def capture(_conn, _cursor, statement, parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        response = client.get("/api/sessions/search", params={"q": "needle", "limit": 2})
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert response.status_code == 200
    assert response.json()["next_offset"] == 2
    assert len(statements) <= 2
    content_queries = [(sql, parameters) for sql, parameters in statements
                       if "agent_messages.content" in sql and "substr(" not in sql]
    assert content_queries
    assert all("LIMIT" in sql.upper() for sql, _ in content_queries)


@pytest.mark.parametrize("params", [{"q": "x" * 201}, {"q": "x", "limit": 0},
                                    {"q": "x", "limit": 101},
                                    {"q": "x", "offset": -1}, {}])
def test_search_validates_query_and_pagination(search_app, params):
    client, _, _ = search_app
    assert client.get("/api/sessions/search", params=params).status_code == 422


def test_search_requires_authenticated_principal_with_agent_read(search_app):
    client, _, app = search_app
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        tenant_id="tenant_1", user_id="user_1", scopes=("agent:invoke",),
    )
    assert client.get("/api/sessions/search", params={"q": "needle"}).status_code == 403
    del app.dependency_overrides[get_current_principal]
    assert client.get("/api/sessions/search", params={"q": "needle"}).status_code == 401
