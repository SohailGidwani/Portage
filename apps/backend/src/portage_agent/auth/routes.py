"""Auth HTTP surface: GitHub OAuth dance, session refresh/logout, API-key management.

The refresh token never reaches JS: it lives in an httpOnly cookie scoped to
`/auth`, so only these endpoints ever see it. The access JWT is returned in JSON and
kept in frontend memory (dies with the tab — that's the point).
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from portage_agent.config import settings
from portage_agent.db.models import User

from . import github, service
from .deps import current_user
from .tokens import mint_access_token

log = logging.getLogger("portage.auth")

router = APIRouter(prefix="/auth", tags=["auth"])

_COOKIE = "portage_refresh"


def _cookie_kwargs() -> dict:
    return {
        "httponly": True,
        "secure": settings.frontend_origin.startswith("https"),
        "samesite": "lax",  # top-level redirect from GitHub must carry it
        "path": "/auth",
        "max_age": settings.refresh_token_ttl_days * 86400,
    }


def _redirect_uri(request: Request) -> str:
    return str(request.url_for("github_callback"))


@router.get("/github/login")
async def github_login(request: Request) -> RedirectResponse:
    if settings.auth_mode != "github":
        raise HTTPException(status_code=404, detail="auth disabled in this deployment")
    if not settings.github_client_id:
        raise HTTPException(status_code=503, detail="GitHub OAuth not configured")
    return RedirectResponse(github.login_url(_redirect_uri(request)))


@router.get("/github/callback", name="github_callback")
async def github_callback(request: Request, code: str = "", state: str = "") -> RedirectResponse:
    if settings.auth_mode != "github":
        raise HTTPException(status_code=404, detail="auth disabled in this deployment")
    if not code or not github.check_state(state):
        raise HTTPException(status_code=400, detail="invalid oauth state")
    identity = await github.exchange_code(code, _redirect_uri(request))
    if identity is None:
        raise HTTPException(status_code=502, detail="GitHub code exchange failed")
    user = await service.upsert_github_user(**identity)
    if user.disabled:
        raise HTTPException(status_code=403, detail="account disabled")
    refresh = await service.issue_refresh(user.id)
    resp = RedirectResponse(settings.frontend_origin)
    resp.set_cookie(_COOKIE, refresh, **_cookie_kwargs())
    return resp


@router.post("/refresh")
async def refresh_session(request: Request, response: Response) -> dict:
    """Rotate the refresh cookie; return a fresh access token (the frontend's heartbeat)."""
    plaintext = request.cookies.get(_COOKIE, "")
    if not plaintext:
        raise HTTPException(status_code=401, detail="no session")
    rotated = await service.rotate_refresh(plaintext)
    if rotated is None:
        response.delete_cookie(_COOKIE, path="/auth")
        raise HTTPException(status_code=401, detail="session expired or reused")
    user, new_refresh = rotated
    response.set_cookie(_COOKIE, new_refresh, **_cookie_kwargs())
    return {
        "access_token": mint_access_token(user_id=user.id, login=user.login, role=user.role),
        "user": {"login": user.login, "role": user.role, "avatar_url": user.avatar_url},
    }


@router.post("/logout")
async def logout(request: Request, response: Response) -> dict:
    plaintext = request.cookies.get(_COOKIE, "")
    if plaintext:
        await service.revoke_refresh(plaintext)
    response.delete_cookie(_COOKIE, path="/auth")
    return {"ok": True}


@router.get("/me")
async def me(user: User = Depends(current_user)) -> dict:
    return {"login": user.login, "role": user.role, "avatar_url": user.avatar_url,
            "auth_mode": settings.auth_mode}


@router.post("/keys", status_code=201)
async def create_key(body: dict, user: User = Depends(current_user)) -> dict:
    name = str(body.get("name") or "unnamed")
    plaintext = await service.create_api_key(user.id, name)
    return {"name": name, "key": plaintext,
            "note": "store this now — it is shown exactly once"}


@router.get("/keys")
async def list_keys(user: User = Depends(current_user)) -> list[dict]:
    return [
        {"id": str(k.id), "name": k.name, "created_at": k.created_at.isoformat(),
         "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None}
        for k in await service.list_api_keys(user.id)
    ]


@router.delete("/keys/{key_id}")
async def revoke_key(key_id: uuid.UUID, user: User = Depends(current_user)) -> dict:
    if not await service.revoke_api_key(user.id, key_id):
        raise HTTPException(status_code=404, detail="key not found")
    return {"ok": True}
