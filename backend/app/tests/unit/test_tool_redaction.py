from dataclasses import dataclass

from app.tools.redaction import redact_sensitive_data


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
