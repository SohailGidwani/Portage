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
    # Deliberate sleep in the graph's middle node so we can kill the worker mid-run.
    work_sleep_seconds: int = 60

    # --- Checkpointer pool ---
    checkpointer_pool_max_size: int = 5

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
