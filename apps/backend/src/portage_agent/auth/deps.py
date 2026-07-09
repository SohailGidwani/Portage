"""FastAPI dependencies: the single `current_user` gate every protected route uses.

Resolution order:
  1. AUTH_MODE=disabled -> a synthetic local admin (created once). Dev flows, DoD
     scripts, and the compose demo keep working with zero ceremony.
  2. `Authorization: Bearer pk_…` -> API key lookup (machines).
  3. `Authorization: Bearer <jwt>` -> access-token claims (browser).
Anything else in github mode -> 401.
"""

from __future__ import annotations

import uuid

from fastapi import Depends, HTTPException, Request

from portage_agent.config import settings
from portage_agent.db.models import User

from . import service
from .tokens import verify_access_token

_LOCAL_ADMIN_GITHUB_ID = -1
_local_admin_cache: User | None = None


async def _local_admin() -> User:
    global _local_admin_cache
    if _local_admin_cache is None:
        _local_admin_cache = await service.upsert_github_user(
            github_id=_LOCAL_ADMIN_GITHUB_ID, login="local", avatar_url=None
        )
        if _local_admin_cache.role != "admin":
            # Promote once; kept out of the normal upsert path on purpose.
            from sqlalchemy import update

            from portage_agent.db.models import User as U
            from portage_agent.db.session import AsyncSessionLocal

            async with AsyncSessionLocal() as session, session.begin():
                await session.execute(
                    update(U).where(U.id == _local_admin_cache.id).values(role="admin")
                )
            _local_admin_cache.role = "admin"
    return _local_admin_cache


async def current_user(request: Request) -> User:
    if settings.auth_mode == "disabled":
        return await _local_admin()

    header = request.headers.get("authorization", "")
    token = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else ""
    if not token:
        raise HTTPException(status_code=401, detail="authentication required")

    if token.startswith("pk_"):
        user = await service.verify_api_key(token)
        if user is None:
            raise HTTPException(status_code=401, detail="invalid or revoked API key")
        return user

    claims = verify_access_token(token)
    if not claims or claims.get("purpose") == "oauth-state":
        raise HTTPException(status_code=401, detail="invalid or expired token")
    user = await service.get_user(uuid.UUID(claims["sub"]))
    if user is None or user.disabled:
        raise HTTPException(status_code=401, detail="unknown or disabled user")
    return user


async def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return user
