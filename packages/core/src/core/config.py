"""Shared configuration settings loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Postgres & Redis (defaults use 5439/6389 to avoid local port collisions)
    database_url: str = "postgresql://app_rw:app_rw_dev_password@127.0.0.1:5439/contextstack"
    api_database_url: str | None = None
    database_admin_url: str = "postgresql://postgres:postgres@127.0.0.1:5439/contextstack"
    redis_url: str = "redis://127.0.0.1:6389/0"

    @property
    def effective_api_database_url(self) -> str:
        """Database DSN for FastAPI (prefer strict no-bypass `api_rw` when configured)."""
        return self.api_database_url or self.database_url

    # Auth & MCP (>=32 bytes for HS256); dev persona tokens are opt-in (fail-closed by default)
    jwt_signing_secret: str = "dev-only-jwt-secret-key-at-least-32-bytes-long"
    mcp_token_secret: str = "dev-only-mcp-secret-key-at-least-32-bytes-long"
    enable_dev_tokens: bool = False
    mcp_infra_url: str = "http://127.0.0.1:8101/mcp"
    mcp_ticketing_url: str = "http://127.0.0.1:8102/mcp"
    mcp_billing_url: str = "http://127.0.0.1:8103/mcp"
    mcp_transport: Literal["direct", "http"] = "direct"

    # Model layer
    model_mode: Literal["mock", "gateway"] = "mock"
    litellm_url: str = "http://127.0.0.1:4000/v1"
    litellm_master_key: str = "sk-litellm-dev-master"

    # Telemetry
    telemetry_backend: Literal["none", "langfuse_otel", "langfuse_callback", "langsmith"] = "none"
    langfuse_base_url: str = "http://127.0.0.1:3000"
    langfuse_public_key: str = "pk-lf-dev-local"
    langfuse_secret_key: str = "sk-lf-dev-local"

    # Resource bounds (spec §5.6)
    max_graph_steps: int = 40
    max_model_calls: int = 12
    max_tool_calls: int = 15
    model_timeout_s: float = 60.0
    model_max_tokens: int = 4096
    tool_timeout_s: float = 10.0
    max_tool_output_chars: int = 8000
    run_timeout_s: float = 300.0
    worker_lease_s: int = 30
    worker_heartbeat_s: int = 10


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
