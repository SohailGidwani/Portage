"""Central configuration.

12-factor: everything comes from the environment (see repo-root `.env.example`).

We talk to a single Postgres from two drivers and derive both DSNs here:
  * ``sqlalchemy_dsn`` (``postgresql+asyncpg://``) — domain tables (SQLAlchemy/Alembic).
  * ``psycopg_dsn``    (``postgresql://``)         — LangGraph's AsyncPostgresSaver
    (it uses psycopg3, not asyncpg). Same database, different driver, no conflict.
"""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # --- Postgres connection ---
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "portage"
    postgres_user: str = "portage"
    postgres_password: str = "portage"

    # --- Worker / queue tuning ---
    # The lease: a 'running' job whose heartbeat is older than this is considered
    # orphaned (its worker crashed) and may be re-claimed. This is what makes
    # "kill the worker, it resumes" work and stay multi-worker-safe.
    job_lease_seconds: int = 15
    heartbeat_interval_seconds: int = 5
    worker_poll_interval_seconds: float = 2.0
    worker_id: str = "worker-1"

    # --- Agent ---
    # Default pre-test delay in the Verify node (seconds). 0 in normal runs; the
    # crash-recovery demo raises it (via job config) to get a window to kill the worker
    # mid-Verify and prove resume skips the already-done Ingest.
    verify_pre_delay_seconds: int = 0

    # --- Checkpointer pool ---
    checkpointer_pool_max_size: int = 5

    # --- Phase 1: workspaces / sandbox / retrieval ---
    # Shared named volume (compose-prefixed) holding per-job workspaces, mounted in both
    # the worker and the ephemeral sandbox at `workspaces_mount`.
    workspaces_volume: str = "portage_workspaces"
    workspaces_mount: str = "/workspaces"
    # Ephemeral test-runner image (built via the compose `tools` profile).
    sandbox_image: str = "portage-sandbox:latest"
    sandbox_cpus: str = "1.0"
    sandbox_memory: str = "512m"
    sandbox_pids_limit: int = 256
    sandbox_timeout_seconds: int = 600
    # code-review-graph MCP server command (installed isolated in the worker image).
    crg_command: str = "code-review-graph"

    # Artifact storage (LocalStorage). On the shared volume so reports survive the job.
    artifacts_dir: str = "/workspaces/_artifacts"

    # --- Phase 2: LLM (LiteLLM model ladder) ---
    # The provider is config, not code: the documented default is Claude Sonnet 4.6 on
    # Bedrock (plan §5), but any LiteLLM model string + matching provider creds in .env
    # works — e.g. `azure/<deployment>` (+ AZURE_API_*), `gemini/<model>` (+ GEMINI_API_KEY),
    # `anthropic/<model>` (+ ANTHROPIC_API_KEY). Swapping providers is an env change.
    llm_driver_model: str = "bedrock/us.anthropic.claude-sonnet-4-6-v1:0"
    # Escalation tier is wired in Phase 3 (a measured recovery strategy); declared here so
    # the ladder is configurable from the start.
    llm_escalation_model: str = "bedrock/us.anthropic.claude-opus-4-8-v1:0"
    llm_max_tokens: int = 4096
    llm_timeout_seconds: int = 120
    # LiteLLM-level transient-error retries (network/5xx) — NOT the agent's task retry.
    llm_request_max_retries: int = 2
    # Opus 4.7+ dropped temperature/top_p/top_k (prompt-steer only). None => omit the param;
    # combined with litellm drop_params=True this is safe across providers.
    llm_temperature: float | None = 0.0

    # --- Phase 2: Execute / migration ---
    # Bounded Execute↔Verify loop (Phase 2 has no rich recovery): the migration is retried
    # at most this many times, feeding the failing test output back to the model. Phase 3
    # replaces this with the full Recover taxonomy + model escalation.
    max_execute_attempts: int = 2
    # Optional per-task delay in Execute (seconds) — a deterministic window to kill the
    # worker mid-Execute and prove content-hash resume skips already-applied tasks. 0 normally.
    execute_task_delay_seconds: int = 0

    @property
    def _userinfo(self) -> str:
        return f"{quote_plus(self.postgres_user)}:{quote_plus(self.postgres_password)}"

    @property
    def _hostpart(self) -> str:
        return f"{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"

    @property
    def sqlalchemy_dsn(self) -> str:
        """Async SQLAlchemy DSN (asyncpg driver) — domain tables."""
        return f"postgresql+asyncpg://{self._userinfo}@{self._hostpart}"

    @property
    def psycopg_dsn(self) -> str:
        """psycopg3 DSN — LangGraph checkpointer tables."""
        return f"postgresql://{self._userinfo}@{self._hostpart}"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
