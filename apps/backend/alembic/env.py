"""Alembic environment — async engine, URL injected from Settings.

Alembic owns the Portage *domain* tables only. LangGraph's checkpoint tables are
created and migrated by its own checkpointer (``AsyncPostgresSaver.setup()``) — same
database, separate ownership, no conflict.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

from portage_agent.config import settings
from portage_agent.db.base import Base

# Import models so their tables register on Base.metadata.
from portage_agent.db import models  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    # Offline ('--sql') mode: strip the async driver, emit literal SQL.
    url = settings.sqlalchemy_dsn.replace("+asyncpg", "")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    cfg = config.get_section(config.config_ini_section, {})
    cfg["sqlalchemy.url"] = settings.sqlalchemy_dsn
    engine = async_engine_from_config(cfg, prefix="sqlalchemy.", poolclass=NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
