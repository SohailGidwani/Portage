"""A thin service layer over models — gives the graph a caller of create_user."""

from __future__ import annotations

from .models import User, create_user


def register_user(name: str, email: str) -> dict[str, str]:
    """Register a user and return a serializable record. Calls create_user."""
    user: User = create_user(name, email)
    return user.model_dump()
