"""Configuration from environment. Secrets are SecretStr and never logged."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # BOTAI_ENV_FILE lets a git worktree use the main checkout's env file (testing.md)
        env_file=tuple(p for p in (".env", "../.env", os.environ.get("BOTAI_ENV_FILE")) if p),
        env_prefix="",
        extra="ignore",
        case_sensitive=False,
    )

    env: Literal["dev", "test", "prod"] = "dev"
    database_url: str = "postgresql+asyncpg://botai:botai@127.0.0.1:55432/botai"
    public_base_url: str = "http://localhost:5173"
    static_dir: str | None = None  # built PWA (apps/web/dist) served by the API in production

    # Master key (KEK) for the Vault: base64 of 32 random bytes. Never stored in the DB.
    botai_kek: SecretStr = SecretStr("")
    botai_kek_id: str = "k1"

    llm_provider: Literal["deepseek", "mock"] = "mock"
    deepseek_api_key: SecretStr = SecretStr("")
    llm_base_url: str = "https://api.deepseek.com"
    llm_model_fast: str = "deepseek-flash"
    llm_model_pro: str = "deepseek-v4-pro"
    # Profiles per purpose: "model:thinking:effort" (agent-harness.md §2). Benchmark tunes these.
    llm_profile_classify: str = "deepseek-flash:disabled:-"
    llm_profile_chat: str = "deepseek-flash:enabled:low"
    llm_profile_learn: str = "deepseek-flash:enabled:high"
    llm_profile_repair: str = "deepseek-flash:enabled:high"
    llm_profile_extract: str = "deepseek-flash:enabled:low"
    llm_debug_traces: bool = False  # reasoning_content to DEBUG logs; dev only

    # Outbound traffic to universities goes through this CONNECT proxy (EgressProxy).
    egress_proxy_url: str | None = None

    session_cookie_name: str = "botai_session"
    session_ttl_days: int = 30

    quota_tokens_per_day: int = 2_000_000
    quota_requests_per_minute: int = 30
    # Whole-pool daily spend cap on the shared key (security.md T7). 0 disables the cap.
    quota_pool_usd_per_day: float = 3.0
    # Failed BotAI logins allowed per (client, e-mail) in a 15-minute window.
    login_failures_per_window: int = 10


@lru_cache
def get_settings() -> Settings:
    return Settings()
