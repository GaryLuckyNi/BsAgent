from app.graph.checkpoints import build_graph_checkpointer, close_graph_checkpointer


def main() -> None:
    try:
        build_graph_checkpointer()
    finally:
        close_graph_checkpointer()


if __name__ == "__main__":
    main()
