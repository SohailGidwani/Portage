"""Token primitives: access JWTs + opaque refresh/API tokens.

Access JWT: HS256, 15-min TTL, claims {sub: user_id, login, role}. Short TTL is what
makes secret rotation and revocation-by-refresh workable without a denylist.

Refresh tokens and API keys are opaque 256-bit random strings; only their SHA-256 lands
in the DB (high-entropy tokens don't need a slow hash — unlike passwords, they can't be
dictionary-attacked; the security property is revocability).
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import jwt as pyjwt

from portage_agent.config import settings


def mint_access_token(*, user_id: uuid.UUID, login: str, role: str) -> str:
    now = datetime.now(UTC)
    return pyjwt.encode(
        {
            "sub": str(user_id),
            "login": login,
            "role": role,
            "iat": now,
            "exp": now + timedelta(seconds=settings.access_token_ttl_seconds),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )


def verify_access_token(token: str) -> dict | None:
    """Decoded claims, or None for anything invalid/expired (callers 401)."""
    try:
        return pyjwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except pyjwt.PyJWTError:
        return None


def new_opaque_token(prefix: str) -> tuple[str, str]:
    """(plaintext, sha256hex). Plaintext is shown/set exactly once, never stored."""
    plaintext = f"{prefix}{secrets.token_urlsafe(32)}"
    return plaintext, hash_token(plaintext)


def hash_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()
