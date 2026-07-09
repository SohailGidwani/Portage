"""Auth persistence operations: users, refresh-token rotation, API keys.

The rotation contract (the part worth testing hard):
  * `issue_refresh` starts a new family (one browser session).
  * `rotate_refresh` exchanges a live token for a new one in the same family and revokes
    the old row. Presenting a token that is already revoked/rotated is REUSE — someone
    (the user, or a thief) is replaying an old token — and the entire family is revoked:
    both parties get logged out, damage stops there.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from portage_agent.config import settings
from portage_agent.db.models import ApiKey, RefreshToken, User
from portage_agent.db.session import AsyncSessionLocal

from .tokens import hash_token, new_opaque_token

log = logging.getLogger("portage.auth")


# ------------------------------------------------------------------------------ users
async def upsert_github_user(
    *, github_id: int, login: str, avatar_url: str | None
) -> User:
    async with AsyncSessionLocal() as session, session.begin():
        user = (
            await session.execute(select(User).where(User.github_id == github_id))
        ).scalar_one_or_none()
        if user is None:
            user = User(
                id=uuid.uuid4(), github_id=github_id, login=login,
                avatar_url=avatar_url, role="user",
            )
            session.add(user)
            log.info("new user via github oauth: %s (gh:%s)", login, github_id)
        else:
            user.login = login
            user.avatar_url = avatar_url
        await session.flush()
        session.expunge(user)
    return user


async def get_user(user_id: uuid.UUID) -> User | None:
    async with AsyncSessionLocal() as session:
        user = (
            await session.execute(select(User).where(User.id == user_id))
        ).scalar_one_or_none()
        if user is not None:
            session.expunge(user)
        return user


# --------------------------------------------------------------------- refresh tokens
async def issue_refresh(user_id: uuid.UUID) -> str:
    """Start a new session family; return the plaintext refresh token (cookie value)."""
    plaintext, digest = new_opaque_token("rt_")
    async with AsyncSessionLocal() as session, session.begin():
        session.add(RefreshToken(
            id=uuid.uuid4(), user_id=user_id, family_id=uuid.uuid4(),
            token_hash=digest,
            expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_ttl_days),
        ))
    return plaintext


async def rotate_refresh(plaintext: str) -> tuple[User, str] | None:
    """Exchange a live refresh token for (user, new plaintext token) in the same family.

    None => invalid/expired/reused; the caller clears the cookie and 401s. Reuse of a
    rotated token revokes the entire family (containment)."""
    digest = hash_token(plaintext)
    async with AsyncSessionLocal() as session, session.begin():
        row = (
            await session.execute(
                select(RefreshToken).where(RefreshToken.token_hash == digest)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        now = datetime.now(UTC)
        if row.revoked:
            # REUSE detected: kill the whole family, both holders lose the session.
            await session.execute(
                update(RefreshToken)
                .where(RefreshToken.family_id == row.family_id)
                .values(revoked=True)
            )
            log.warning("refresh-token REUSE detected; family %s revoked", row.family_id)
            return None
        if row.expires_at <= now:
            return None
        user = (
            await session.execute(select(User).where(User.id == row.user_id))
        ).scalar_one_or_none()
        if user is None or user.disabled:
            return None
        row.revoked = True  # rotated away
        new_plain, new_digest = new_opaque_token("rt_")
        session.add(RefreshToken(
            id=uuid.uuid4(), user_id=user.id, family_id=row.family_id,
            token_hash=new_digest,
            expires_at=now + timedelta(days=settings.refresh_token_ttl_days),
        ))
        session.expunge(user)
    return user, new_plain


async def revoke_refresh(plaintext: str) -> None:
    """Logout: revoke the presented token's whole family."""
    digest = hash_token(plaintext)
    async with AsyncSessionLocal() as session, session.begin():
        row = (
            await session.execute(
                select(RefreshToken).where(RefreshToken.token_hash == digest)
            )
        ).scalar_one_or_none()
        if row is not None:
            await session.execute(
                update(RefreshToken)
                .where(RefreshToken.family_id == row.family_id)
                .values(revoked=True)
            )


# -------------------------------------------------------------------------- api keys
async def create_api_key(user_id: uuid.UUID, name: str) -> str:
    """Create a key; return the plaintext ONCE (only the hash is stored)."""
    plaintext, digest = new_opaque_token("pk_")
    async with AsyncSessionLocal() as session, session.begin():
        session.add(ApiKey(id=uuid.uuid4(), user_id=user_id, name=name[:64], key_hash=digest))
    return plaintext


async def verify_api_key(plaintext: str) -> User | None:
    digest = hash_token(plaintext)
    async with AsyncSessionLocal() as session, session.begin():
        key = (
            await session.execute(select(ApiKey).where(ApiKey.key_hash == digest))
        ).scalar_one_or_none()
        if key is None or key.revoked:
            return None
        key.last_used_at = datetime.now(UTC)
        user = (
            await session.execute(select(User).where(User.id == key.user_id))
        ).scalar_one_or_none()
        if user is None or user.disabled:
            return None
        session.expunge(user)
    return user


async def list_api_keys(user_id: uuid.UUID) -> list[ApiKey]:
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(ApiKey).where(ApiKey.user_id == user_id, ApiKey.revoked.is_(False))
                .order_by(ApiKey.created_at.desc())
            )
        ).scalars().all()
        for r in rows:
            session.expunge(r)
        return list(rows)


async def revoke_api_key(user_id: uuid.UUID, key_id: uuid.UUID) -> bool:
    async with AsyncSessionLocal() as session, session.begin():
        key = (
            await session.execute(
                select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user_id)
            )
        ).scalar_one_or_none()
        if key is None:
            return False
        key.revoked = True
        return True
