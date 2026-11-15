from dataclasses import dataclass

from app.tools.redaction import redact_sensitive_data, sanitize_untrusted_details


def test_redaction_matches_credential_phrases_without_obvious_false_positives():
    value = {
        "database_password": "db-secret",
        "oauth_access_token": "oauth-secret",
        "clientSecret": "client-secret",
        "private-key": "private-secret",
        "token_count": 12,
        "secretary_name": "Alice",
        "monkey": "value",
    }

    assert redact_sensitive_data(value) == {
        "database_password": "***",
        "oauth_access_token": "***",
        "clientSecret": "***",
        "private-key": "***",
        "token_count": 12,
        "secretary_name": "Alice",
        "monkey": "value",
    }


def test_redaction_recurses_through_json_schema_composition_and_containers():
    schema = {
        "$defs": {
            "Profile": {
                "type": "object",
                "properties": {
                    "pin": {"type": "string", "x-sensitive": True},
                },
            },
            "Item": {
                "type": "object",
                "properties": {
                    "credential": {"type": "string", "writeOnly": True},
                },
            },
        },
        "definitions": {
            "Choice": {"type": "string", "x-sensitive": True},
        },
        "type": "object",
        "properties": {
            "profile": {"allOf": [{"$ref": "#/$defs/Profile"}]},
            "items": {
                "type": "array",
                "items": {"anyOf": [{"$ref": "#/$defs/Item"}]},
            },
            "labels": {
                "type": "object",
                "additionalProperties": {"x-sensitive": True},
            },
            "patterned": {
                "type": "object",
                "patternProperties": {
                    "^secure_": {"writeOnly": True},
                },
            },
            "choice": {
                "oneOf": [
                    {"type": "string"},
                    {"$ref": "#/definitions/Choice"},
                ]
            },
        },
    }
    value = {
        "profile": {"pin": "1234"},
        "items": [{"credential": "item-secret"}],
        "labels": {"region": "internal"},
        "patterned": {"secure_note": "hidden", "public_note": "visible"},
        "choice": "choice-secret",
    }

    assert redact_sensitive_data(value, schema=schema) == {
        "profile": {"pin": "***"},
        "items": [{"credential": "***"}],
        "labels": {"region": "***"},
        "patterned": {"secure_note": "***", "public_note": "visible"},
        "choice": "***",
    }


def test_redaction_handles_cyclic_local_refs_without_recursing_forever():
    schema = {
        "$defs": {
            "Node": {
                "type": "object",
                "properties": {
                    "value": {"type": "string"},
                    "next": {"$ref": "#/$defs/Node"},
                },
            }
        },
        "$ref": "#/$defs/Node",
    }

    assert redact_sensitive_data(
        {"value": "visible", "next": {"database_password": "hidden"}},
        schema=schema,
    ) == {"value": "visible", "next": {"database_password": "***"}}


def test_redaction_normalizes_dataclass_and_set_containers():
    @dataclass
    class Credentials:
        client_secret: str
        labels: set[str]

    redacted = redact_sensitive_data(
        {
            "payload": Credentials(
                client_secret="dataclass-secret",
                labels={"visible"},
            ),
            "items": ({"database_password": "tuple-secret"},),
        }
    )

    assert redacted == {
        "payload": {"client_secret": "***", "labels": ["visible"]},
        "items": [{"database_password": "***"}],
    }


def test_redaction_applies_prefix_items_with_composed_local_ref():
    schema = {
        "$defs": {
            "SensitiveValue": {"type": "string", "x-sensitive": True},
        },
        "allOf": [
            {
                "type": "array",
                "prefixItems": [
                    {"type": "string"},
                    {"$ref": "#/$defs/SensitiveValue"},
                ],
                "items": {"type": "string"},
            }
        ],
    }

    assert redact_sensitive_data(["visible", "hidden", "tail"], schema=schema) == [
        "visible",
        "***",
        "tail",
    ]


def test_redaction_fails_closed_for_unresolved_local_ref_and_unknown_security_shape():
    assert redact_sensitive_data(
        {"value": "must-not-leak"},
        schema={"$ref": "#/$defs/Missing"},
    ) == "***"
    assert redact_sensitive_data(
        "must-not-leak",
        schema={"x-sensitive": {"when": "production"}},
    ) == "***"


def test_redaction_stops_recursive_runtime_objects():
    value = {"name": "visible"}
    value["self"] = value

    assert redact_sensitive_data(value) == {"name": "visible", "self": "***"}


def test_sanitize_untrusted_details_redacts_sensitive_keys_and_credential_text():
    details = {
        "missing_fields": ["pe_ttm"],
        "reason": "field_selection_invalid",
        "http_status": 403,
        "token": "top-secret-token",
        "nested": {
            "password": "db-secret",
            "authorization": "Bearer secret-in-sensitive-key",
            "endpoint": "postgresql://agent:db-pass@postgres/agent",
            "messages": [
                "Authorization: Bearer abcdefghijklmnop",
                "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyLTEifQ.signaturepart123",
                "ordinary short value",
            ],
        },
    }

    assert sanitize_untrusted_details(details) == {
        "missing_fields": ["pe_ttm"],
        "reason": "field_selection_invalid",
        "http_status": 403,
        "token": "***",
        "nested": {
            "password": "***",
            "authorization": "***",
            "endpoint": "postgresql://***@postgres/agent",
            "messages": [
                "Authorization: ***",
                "***",
                "ordinary short value",
            ],
        },
    }


def test_sanitize_untrusted_details_handles_cycles_and_returns_json_safe_dict():
    details = {"reason": "safe"}
    details["self"] = details

    assert sanitize_untrusted_details(details) == {
        "reason": "safe",
        "self": "***",
    }


def test_sanitize_untrusted_details_only_redacts_sensitive_assignments():
    assert sanitize_untrusted_details(
        {
            "diagnostic": (
                "symbol=300085.SZ reason=field_selection_invalid "
                "access_token=secret-access password:db-pass "
                "client_secret = client-secret database_url:postgresql://u:p@db/app"
            )
        }
    ) == {
        "diagnostic": (
            "symbol=300085.SZ reason=field_selection_invalid "
            "access_token=*** password:*** "
            "client_secret = *** database_url:***"
        )
    }


def test_sanitize_untrusted_details_enforces_depth_node_collection_and_string_limits():
    deep: dict = {"value": "kept"}
    for _ in range(12):
        deep = {"nested": deep}

    result = sanitize_untrusted_details(
        {
            "deep": deep,
            "long": "x" * 3000,
            "many": list(range(150)),
            "nodes": [{"value": index} for index in range(300)],
        }
    )

    assert "[depth_limit]" in str(result["deep"])
    assert result["long"] == "x" * 2048 + "[string_limit]"
    assert len(result["many"]) == 101
    assert result["many"][-1] == "[collection_limit]"
    assert "[node_limit]" in str(result["nodes"])
