import pytest

from app.operations.lifecycle import close_resources


def test_close_resources_continues_after_a_closer_fails():
    calls: list[str] = []

    def failing_close() -> None:
        calls.append("failing")
        raise RuntimeError("close failed")

    def later_close() -> None:
        calls.append("later")

    with pytest.raises(RuntimeError, match="close failed"):
        close_resources(failing_close, later_close)

    assert calls == ["failing", "later"]
