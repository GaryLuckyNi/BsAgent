import sys
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    service_name: str = "bs-agent-service"
    rag_base_url: str = "http://localhost:8001"
    rag_timeout_seconds: float = 30.0
    use_mock_rag: bool = False
    database_url: str = "sqlite:///./bs_agent.db"
    redis_url: str = "redis://localhost:6379/0"
    short_term_memory_backend: Literal["none", "redis"] = "none"
    short_term_memory_ttl_seconds: int = 3600
    short_term_memory_max_events: int = 100
    graph_checkpointer_backend: Literal["memory", "none", "sqlite"] = "sqlite"
    graph_checkpoint_sqlite_path: str = "./bs_agent_checkpoints.sqlite"
    skill_registry_base_url: str = "http://localhost:8010"
    skill_definition_cache_ttl_seconds: int = 300
    mcp_server_command: str = sys.executable
    mcp_server_args: list[str] = ["-m", "app.main"]
    mcp_server_cwd: str = "./mcp server"
    mcp_server_name: str = "mock"
    mcp_timeout_seconds: float = 30.0
    llm_provider: Literal["deepseek"] = "deepseek"
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"
    llm_timeout_seconds: float = 30.0

    model_config = SettingsConfigDict(env_prefix="BS_AGENT_")


settings = Settings()
