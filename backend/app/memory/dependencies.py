from fastapi import Request
from langgraph.store.base import BaseStore

from app.memory.runtime import PostgresStoreRuntime


def get_memory_store(request: Request) -> BaseStore:
    runtime: PostgresStoreRuntime = request.app.state.memory_store_runtime
    return runtime.get()
