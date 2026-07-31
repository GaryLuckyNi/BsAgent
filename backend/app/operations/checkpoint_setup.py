from app.core.config import settings
from app.graph.checkpoints import build_graph_checkpointer, close_graph_checkpointer


def main() -> None:
    if settings.graph_checkpointer_backend != "postgres":
        raise RuntimeError("生产迁移任务必须使用 PostgreSQL checkpointer。")
    try:
        build_graph_checkpointer()
    finally:
        close_graph_checkpointer()


if __name__ == "__main__":
    main()
