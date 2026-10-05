from io import BytesIO

import pytest
from app.operations.langfuse_content import LangfuseContentPolicy, sanitize_content


@pytest.mark.parametrize(
    "key",
    ["Authorization", "COOKIE", "api_key", "secret", "password", "token"],
)
def test_removes_sensitive_keys_case_insensitively(key):
    assert sanitize_content({key: "private", "safe": 1}) == {"safe": 1}


def test_recursively_sanitizes_without_mutating_original_containers():
    original = {"items": [{"TOKEN": "secret", "value": "ok"}], "tuple": ("safe",)}
    result = sanitize_content(original)
    assert result == {"items": [{"value": "ok"}], "tuple": ("safe",)}
    assert original["items"][0]["TOKEN"] == "secret"


def test_reasoning_fields_are_removed_by_default_and_local_debug_requires_opt_in():
    payload = {"reasoning": "thought", "reasoning_content": "private"}
    assert sanitize_content(payload) == {}
    assert sanitize_content(payload, mode="local_debug") == {}
    assert sanitize_content(payload, mode="local_debug", capture_reasoning=True) == payload
    assert sanitize_content(payload, mode="cloud", capture_reasoning=True) == {}


def test_truncates_long_strings_with_original_length_and_keeps_short_strings():
    assert sanitize_content("hello", max_chars=5) == "hello"
    result = sanitize_content("abcdefgh", max_chars=3)
    assert result == {
        "content": "abc",
        "truncated": True,
        "original_chars": 8,
        "retained_chars": 3,
    }


def test_sanitizes_strings_recursively_and_preserves_input():
    original = ["abcdefgh"]
    result = sanitize_content(original, max_chars=3)
    assert result[0] == {
        "content": "abc",
        "truncated": True,
        "original_chars": 8,
        "retained_chars": 3,
    }
    assert original == ["abcdefgh"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            "Fetch https://example.test/path?token=secret&keep=visible",
            "Fetch https://example.test/path?token=[REDACTED]&keep=visible",
        ),
        (
            "https://example.test/?api_key=secret&signature=sig&password=pw&auth=a",
            (
                "https://example.test/?api_key=[REDACTED]&signature=[REDACTED]"
                "&password=[REDACTED]&auth=[REDACTED]"
            ),
        ),
        (
            '{"url":"https://example.test/?token=secret","ok":"visible"}',
            '{"url":"https://example.test/?token=[REDACTED]","ok":"visible"}',
        ),
        (
            "Authorization: Bearer abc.def_123 and key sk-1234567890 pk-abcdefghij",
            "Authorization: [REDACTED] and key [REDACTED] [REDACTED]",
        ),
        (
            "The business tokenization key is useful; skincare is a product line.",
            "The business tokenization key is useful; skincare is a product line.",
        ),
    ],
)
def test_sanitizes_credentials_embedded_in_text(value, expected):
    assert sanitize_content(value) == expected


def test_policy_defaults_to_local_content_and_exposes_both_sanitizers():
    policy = LangfuseContentPolicy(max_chars=4)
    assert policy.mode == "local_content"
    assert policy.sanitize_text("abcdef") == {
        "content": "abcd",
        "truncated": True,
        "original_chars": 6,
        "retained_chars": 4,
    }
    assert policy.sanitize_text("short") == {
        "content": "shor",
        "truncated": True,
        "original_chars": 5,
        "retained_chars": 4,
    }
    assert policy.sanitize_text("ok") == "ok"
    assert policy.sanitize({"value": "abcdef"})["value"] == {
        "content": "abcd",
        "truncated": True,
        "original_chars": 6,
        "retained_chars": 4,
    }


def test_bytes_and_bytearray_return_descriptions_without_content():
    for value in (b"private bytes", bytearray(b"private bytes")):
        result = sanitize_content(value)
        assert isinstance(result, str)
        assert "private bytes" not in result
        assert "bytes" in result.lower()
        assert "13" in result


def test_file_objects_are_described_without_reading():
    class UnreadableFile(BytesIO):
        def read(self, *args, **kwargs):
            raise AssertionError("file content must not be read")

    value = UnreadableFile(b"private")
    result = sanitize_content(value)
    assert isinstance(result, str)
    assert "file" in result.lower()
    assert value.tell() == 0


@pytest.mark.parametrize("max_chars", [0, -1])
def test_rejects_non_positive_max_chars(max_chars):
    with pytest.raises(ValueError):
        sanitize_content("text", max_chars=max_chars)
