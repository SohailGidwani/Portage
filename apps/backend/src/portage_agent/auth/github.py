"""GitHub OAuth (web application flow) — the sole identity provider (rev-C §2.1).

`login_url` sends the browser to GitHub with a signed `state` (a short-TTL JWT — no
server-side state store needed, and it can't be forged or replayed after expiry).
`exchange_code` turns the callback's code into the GitHub user identity. Scope is empty:
identity only, no repo access requested.
"""

from __future__ import annotations

from urllib.parse import urlencode

import httpx
import jwt as pyjwt

from portage_agent.config import settings

from .tokens import verify_access_token

_AUTHORIZE = "https://github.com/login/oauth/authorize"
_TOKEN = "https://github.com/login/oauth/access_token"
_USER_API = "https://api.github.com/user"


def make_state() -> str:
    # Reuse the access-JWT machinery: a 15-min token with a dedicated claim.
    from datetime import UTC, datetime, timedelta

    return pyjwt.encode(
        {"purpose": "oauth-state", "exp": datetime.now(UTC) + timedelta(minutes=15)},
        settings.jwt_secret, algorithm="HS256",
    )


def check_state(state: str) -> bool:
    claims = verify_access_token(state)
    return bool(claims and claims.get("purpose") == "oauth-state")


def login_url(redirect_uri: str) -> str:
    return _AUTHORIZE + "?" + urlencode({
        "client_id": settings.github_client_id,
        "redirect_uri": redirect_uri,
        "state": make_state(),
        "allow_signup": "true",
        # no scopes: public identity only
    })


async def exchange_code(code: str, redirect_uri: str) -> dict | None:
    """code -> {github_id, login, avatar_url}, or None on any failure."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        token_resp = await client.post(
            _TOKEN,
            headers={"Accept": "application/json"},
            data={
                "client_id": settings.github_client_id,
                "client_secret": settings.github_client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
        access = token_resp.json().get("access_token") if token_resp.status_code == 200 else None
        if not access:
            return None
        user_resp = await client.get(
            _USER_API,
            headers={"Authorization": f"Bearer {access}",
                     "Accept": "application/vnd.github+json"},
        )
        if user_resp.status_code != 200:
            return None
        u = user_resp.json()
        if not u.get("id"):
            return None
        return {"github_id": int(u["id"]), "login": u.get("login", "?"),
                "avatar_url": u.get("avatar_url")}
