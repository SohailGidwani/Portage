"""Async engine + session factory (one engine per process)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from portage_agent.config import settings

engine: AsyncEngine = create_async_engine(
    settings.sqlalchemy_dsn,
    pool_pre_ping=True,
    future=True,
)

AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine,
    expire_on_commit=False,
    autoflush=False,
)
