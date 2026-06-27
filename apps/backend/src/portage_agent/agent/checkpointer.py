"""LangGraph Postgres checkpointer wiring.

Uses a *persistent* psycopg AsyncConnectionPool (not per-job ``from_conn_string``). The
connection kwargs mirror what AsyncPostgresSaver.from_conn_string sets internally —
``autocommit=True, prepare_threshold=0, row_factory=dict_row`` — which the saver requires.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from portage_agent.config import settings

log = logging.getLogger("portage.agent")

# Required by AsyncPostgresSaver for every pooled connection.
_CONNECTION_KWARGS = {
    "autocommit": True,
    "prepare_threshold": 0,
    "row_factory": dict_row,
}


@asynccontextmanager
async def open_checkpointer() -> AsyncIterator[AsyncPostgresSaver]:
    """Open a pool, build the saver, run setup() once, yield it. Closes the pool on exit.

    `setup()` creates LangGraph's own checkpoint tables (idempotent) — separate from the
    Alembic-owned domain tables in the same database.
    """
    pool = AsyncConnectionPool(
        conninfo=settings.psycopg_dsn,
        max_size=settings.checkpointer_pool_max_size,
        kwargs=_CONNECTION_KWARGS,
        open=False,
    )
    await pool.open(wait=True)
    try:
        saver = AsyncPostgresSaver(pool)
        await saver.setup()
        log.info("checkpointer ready (pool max_size=%s)", settings.checkpointer_pool_max_size)
        yield saver
    finally:
        await pool.close()
