"""SQLAlchemy declarative base. Alembic owns the migrations for these tables."""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class for all ORM models (Portage domain tables)."""
