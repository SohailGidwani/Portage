"""Domain models. Deliberately has a small call graph so the knowledge graph has
callers/callees/test edges to query (blast-radius)."""

from __future__ import annotations

from pydantic import BaseModel, field_validator


class User(BaseModel):
    name: str
    email: str

    @field_validator("email")
    @classmethod
    def _check_email(cls, v: str) -> str:
        return validate_email(v)


def validate_email(email: str) -> str:
    """Normalize + minimally validate an email. Called by User and create_user."""
    email = email.strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise ValueError(f"invalid email: {email!r}")
    return email


def create_user(name: str, email: str) -> User:
    """Build a User after normalizing the email. A caller of validate_email."""
    return User(name=name.strip(), email=validate_email(email))
