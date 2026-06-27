"""Tests exercising models + service. Pass with pydantic v2 present."""

import pytest

from sample.models import User, create_user, validate_email
from sample.service import register_user


def test_validate_email_normalizes():
    assert validate_email("  Alice@Example.COM ") == "alice@example.com"


def test_validate_email_rejects_bad():
    with pytest.raises(ValueError):
        validate_email("nope")


def test_create_user():
    u = create_user("  Bob ", "BOB@x.io")
    assert isinstance(u, User)
    assert u.name == "Bob"
    assert u.email == "bob@x.io"


def test_register_user_returns_record():
    rec = register_user("Cara", "cara@y.io")
    assert rec == {"name": "Cara", "email": "cara@y.io"}
