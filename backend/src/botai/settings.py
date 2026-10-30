"""Configuration from environment. Secrets are SecretStr and never logged."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
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
    exa_api_key: SecretStr = SecretStr("")
    search_mode: Literal["auto", "off", "exa", "deepseek"] = "auto"
    search_deadline_s: float = Field(default=15.0, ge=0.1, le=60)
    search_max_calls: int = Field(default=4, ge=1, le=8)
    search_max_cost_usd: float = Field(default=0.03, gt=0, le=0.10)
    search_cooldown_s: float = Field(default=60.0, ge=1, le=3600)
    llm_base_url: str = "https://api.deepseek.com"
    llm_model_fast: str = "deepseek-flash"
    llm_model_pro: str = "deepseek-v4-pro"
    # Profiles per purpose: "model:thinking:effort" (agent-harness.md §2). Benchmark tunes these.
    llm_profile_classify: str = "deepseek-flash:disabled:-"
    llm_profile_agent: str = "deepseek-flash:enabled:high"
    llm_profile_learn: str = "deepseek-flash:enabled:high"
    llm_profile_repair: str = "deepseek-flash:enabled:low"
    llm_profile_extract: str = "deepseek-flash:enabled:low"
    llm_debug_traces: bool = False  # legacy configuration accepted; raw reasoning is never logged

    # Operator-only structured diagnostics. This switch never enables logging of raw content.
    botai_diagnostics: bool = False
    botai_diagnostics_dir: str = "/var/log/botai/diagnostics"
    botai_diagnostics_service: Literal["api", "worker", "proxy", "app"] = "app"
    botai_diagnostics_retention_hours: int = Field(default=24, ge=1, le=168)
    botai_diagnostics_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, le=104857600)
    botai_diagnostics_backup_count: int = Field(default=2, ge=0, le=10)

    # Private, versioned research engine (ADR-0011) and its limits.
    botai_research_slice_seconds: float = Field(default=90, gt=0, le=300)
    botai_research_slice_steps: int = Field(default=10, ge=1, le=50)
    botai_research_max_seconds: float = Field(default=1800, gt=0, le=21600)
    botai_research_max_tokens: int = Field(default=1_500_000, ge=1000, le=10_000_000)
    botai_research_max_steps: int = Field(default=100, ge=1, le=1000)
    botai_research_max_requests: int = Field(default=2000, ge=1, le=10000)
    botai_research_checkpoint_ttl_hours: int = Field(default=24, ge=1, le=168)

    # Outbound traffic to universities goes through this CONNECT proxy (EgressProxy).
    egress_proxy_url: str | None = None

    session_cookie_name: str = "botai_session"
    session_ttl_days: int = 30

    # One agent turn may use up to 1.2M tokens (30 model steps with tools), so a day allows many.
    # The loop guard stays: the per-turn budget, the per-minute rate and the pool cap below.
    quota_tokens_per_day: int = 20_000_000
    quota_requests_per_minute: int = 90
    # Whole-pool daily spend cap on the shared key (security.md T7). 0 disables the cap.
    quota_pool_usd_per_day: float = 20.0
    # Failed BotAI logins allowed per (client, e-mail) in a 15-minute window.
    login_failures_per_window: int = 10


_RENAMED = {"LLM_PROFILE_CHAT": "LLM_PROFILE_AGENT"}  # a removed name must not be lost silently


@lru_cache
def get_settings() -> Settings:
    for old, new in _RENAMED.items():
        if os.environ.get(old):
            raise RuntimeError(f"{old} больше не читается: переименуйте его в {new}")
    return Settings()
