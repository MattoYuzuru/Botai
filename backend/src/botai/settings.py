"""Configuration from environment. Secrets are SecretStr and never logged."""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # BOTAI_ENV_FILE lets a git worktree use the main checkout's env file (testing.md)
        env_file=tuple(p for p in (".env", "../.env", os.environ.get("BOTAI_ENV_FILE")) if p),
        env_prefix="",
        extra="ignore",
        case_sensitive=False,
        # Fields with a BOTAI_* validation_alias (the v4 agent settings) are also settable by
        # their Python names, so tests and code do not have to spell the env name.
        populate_by_name=True,
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

    # Private, versioned research engine (v3) and its limits. Deprecated: the v4 engine uses the
    # soft `agent_limits_*` below; this block still drives the current research/sync path and is
    # read by existing env files, so it keeps working until phase 2 is accepted (docs/v4/10-*).
    botai_research_slice_seconds: float = Field(default=90, gt=0, le=300)
    botai_research_slice_steps: int = Field(default=10, ge=1, le=50)
    botai_research_max_seconds: float = Field(default=1800, gt=0, le=21600)
    botai_research_max_tokens: int = Field(default=1_500_000, ge=1000, le=10_000_000)
    botai_research_max_steps: int = Field(default=100, ge=1, le=1000)
    botai_research_max_requests: int = Field(default=2000, ge=1, le=10000)
    botai_research_checkpoint_ttl_hours: int = Field(default=24, ge=1, le=168)
    # A bounded slice that changed nothing is not resumed forever: this many such slices in a row
    # end the task with `source_no_progress` instead of another deferral (production: 15 re-runs,
    # 23 minutes, 2005/2000 requests). 1 disables the tolerance (the second empty slice stops).
    botai_research_no_progress_slices: int = Field(default=3, ge=1, le=100)

    # Which task engine runs: "v3" is the current deterministic research/sync path, "v4" is the
    # free harness (docs/v4/02-architecture.md). The flag lets the new engine be deployed next to
    # the old one and compared on the benchmark; the old path stays until phase 2 is accepted.
    # In phase 0 v4 is scaffolding only: no runtime is wired to this flag yet.
    agent_engine: Literal["v3", "v4"] = Field(default="v3", validation_alias="BOTAI_AGENT_ENGINE")
    # Soft, per-task limits of the v4 engine (D-009): None = no limit; a number is a visible
    # counter/warning, and a stop only under the owner's policy (`agent_auto_continue=False`).
    # These are not architectural walls and do not bound the v3 research path.
    agent_limits_steps: int | None = Field(
        default=None, ge=1, le=100_000, validation_alias="BOTAI_AGENT_LIMITS_STEPS"
    )
    agent_limits_seconds: float | None = Field(
        default=None, gt=0, le=86_400, validation_alias="BOTAI_AGENT_LIMITS_SECONDS"
    )
    agent_limits_tokens: int | None = Field(
        default=None, ge=1000, le=1_000_000_000, validation_alias="BOTAI_AGENT_LIMITS_TOKENS"
    )
    agent_limits_usd: float | None = Field(
        default=None, gt=0, le=1000, validation_alias="BOTAI_AGENT_LIMITS_USD"
    )
    # The emergency stop is not soft: past it the task stops even with `agent_auto_continue`.
    agent_hard_stop_usd: float = Field(
        default=2.0, gt=0, le=1000, validation_alias="BOTAI_AGENT_HARD_STOP_USD"
    )
    # Ask the student before continuing past a soft limit; True = continue automatically.
    agent_auto_continue: bool = Field(default=False, validation_alias="BOTAI_AGENT_AUTO_CONTINUE")

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

# The v3 research/sync path still reads all of these; the v4 engine has soft `agent_limits_*`
# (docs/v4/10-limits-and-research.md). Set names are reported once at startup so a migration is
# visible, but nothing is refused: existing env files must keep working (phase 0).
_DEPRECATED_ENV = (
    "BOTAI_RESEARCH_SLICE_SECONDS",
    "BOTAI_RESEARCH_SLICE_STEPS",
    "BOTAI_RESEARCH_MAX_SECONDS",
    "BOTAI_RESEARCH_MAX_STEPS",
    "BOTAI_RESEARCH_MAX_TOKENS",
    "BOTAI_RESEARCH_MAX_REQUESTS",
    "BOTAI_RESEARCH_CHECKPOINT_TTL_HOURS",
)


@lru_cache
def get_settings() -> Settings:
    for old, new in _RENAMED.items():
        if os.environ.get(old):
            raise RuntimeError(f"{old} больше не читается: переименуйте его в {new}")
    settings = Settings()
    deprecated = sorted(name for name in _DEPRECATED_ENV if os.environ.get(name))
    if deprecated:
        log.warning(
            "deprecated research settings stay honored for the v3 engine: %s; "
            "the v4 engine reads agent_limits_* (docs/v4/10-limits-and-research.md)",
            ", ".join(deprecated),
        )
    return settings
