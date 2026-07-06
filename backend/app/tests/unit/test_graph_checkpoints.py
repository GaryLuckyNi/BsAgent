import sys
from types import ModuleType

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

from app.graph import checkpoints
from app.graph.checkpoints import CheckpointBackendError, build_graph_checkpointer


def test_build_graph_checkpointer_returns_sqlite_saver_by_default(monkeypatch, tmp_path):
    build_graph_checkpointer.cache_clear()
    monkeypatch.setattr(checkpoints.settings, "graph_checkpointer_backend", "sqlite")
    monkeypatch.setattr(
        checkpoints.settings,
        "graph_checkpoint_sqlite_path",
        str(tmp_path / "checkpoints.sqlite"),
    )

    checkpointer = build_graph_checkpointer()

    assert isinstance(checkpointer, SqliteSaver)
    assert (tmp_path / "checkpoints.sqlite").exists()


def test_build_graph_checkpointer_can_use_memory_backend():
    build_graph_checkpointer.cache_clear()

    checkpointer = build_graph_checkpointer(backend="memory")

    assert isinstance(checkpointer, InMemorySaver)


def test_build_graph_checkpointer_can_be_disabled():
    build_graph_checkpointer.cache_clear()

    checkpointer = build_graph_checkpointer(backend="none")

    assert checkpointer is None


def test_build_graph_checkpointer_reuses_same_backend_instance():
    build_graph_checkpointer.cache_clear()

    first = build_graph_checkpointer(backend="memory")
    second = build_graph_checkpointer(backend="memory")

    assert first is second


def test_build_graph_checkpointer_requires_official_sqlite_package(monkeypatch):
    build_graph_checkpointer.cache_clear()
    monkeypatch.delitem(sys.modules, "langgraph.checkpoint.sqlite", raising=False)

    def fake_import_module(name):
        if name == "langgraph.checkpoint.sqlite":
            raise ModuleNotFoundError(name)
        return __import__(name)

    monkeypatch.setattr(checkpoints.importlib, "import_module", fake_import_module)

    with pytest.raises(CheckpointBackendError) as exc_info:
        build_graph_checkpointer(backend="sqlite", sqlite_path="checkpoints.sqlite")

    assert "langgraph-checkpoint-sqlite" in str(exc_info.value)


def test_build_graph_checkpointer_loads_sqlite_saver_lazily(monkeypatch):
    build_graph_checkpointer.cache_clear()

    class FakeSqliteSaver:
        def __init__(self, conn) -> None:
            self.conn = conn
            self.setup_called = False

        def setup(self) -> None:
            self.setup_called = True

    fake_module = ModuleType("langgraph.checkpoint.sqlite")
    fake_module.SqliteSaver = FakeSqliteSaver
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.sqlite", fake_module)

    checkpointer = build_graph_checkpointer(backend="sqlite", sqlite_path=":memory:")

    assert isinstance(checkpointer, FakeSqliteSaver)
    assert checkpointer.setup_called is True
