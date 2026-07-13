"""Refresh-rotation + API-key + JWT contract tests (Phase 7).

These hit the real Postgres (compose `db` on localhost:5432) — the rotation semantics
live in SQL transactions, so mocking the store would test nothing. Run:

    docker compose up -d db api   # api applies migrations
    POSTGRES_HOST=localhost uv run pytest tests/test_auth_service.py -q
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import Response
from sqlalchemy import delete

os.environ.setdefault("POSTGRES_HOST", "localhost")

from portage_agent.api.app import list_jobs  # noqa: E402
from portage_agent.auth import service  # noqa: E402
from portage_agent.auth.tokens import (  # noqa: E402
    mint_access_token,
    verify_access_token,
)
from portage_agent.db.models import Job  # noqa: E402
from portage_agent.db.session import AsyncSessionLocal  # noqa: E402


@pytest.fixture
async def user():
    return await service.upsert_github_user(
        github_id=-int(uuid.uuid4().int % 1_000_000_000) - 2,  # unique, never -1 (local admin)
        login=f"testuser-{uuid.uuid4().hex[:8]}",
        avatar_url=None,
    )


async def test_access_token_roundtrip(user):
    token = mint_access_token(user_id=user.id, login=user.login, role=user.role)
    claims = verify_access_token(token)
    assert claims and claims["sub"] == str(user.id) and claims["role"] == "user"
    assert verify_access_token(token + "tampered") is None


async def test_refresh_rotation_happy_path(user):
    t1 = await service.issue_refresh(user.id)
    rotated = await service.rotate_refresh(t1)
    assert rotated is not None
    u, t2 = rotated
    assert u.id == user.id and t2 != t1
    # the new token keeps working
    rotated2 = await service.rotate_refresh(t2)
    assert rotated2 is not None


async def test_reuse_detection_revokes_family(user):
    t1 = await service.issue_refresh(user.id)
    rotated = await service.rotate_refresh(t1)
    assert rotated is not None
    _, t2 = rotated
    # REPLAY the old token -> reuse detected -> whole family dead
    assert await service.rotate_refresh(t1) is None
    # ...including the newest token in the chain
    assert await service.rotate_refresh(t2) is None


async def test_logout_revokes_family(user):
    t1 = await service.issue_refresh(user.id)
    await service.revoke_refresh(t1)
    assert await service.rotate_refresh(t1) is None


async def test_families_are_independent(user):
    a = await service.issue_refresh(user.id)  # session A (e.g. laptop)
    b = await service.issue_refresh(user.id)  # session B (e.g. phone)
    assert await service.rotate_refresh(a) is not None
    await service.revoke_refresh(b)
    # killing B must not affect A's chain
    rotated = await service.rotate_refresh(await service.issue_refresh(user.id))
    assert rotated is not None


async def test_api_key_lifecycle(user):
    plain = await service.create_api_key(user.id, "ci")
    assert plain.startswith("pk_")
    assert (await service.verify_api_key(plain)).id == user.id
    assert await service.verify_api_key("pk_not-a-real-key") is None
    keys = await service.list_api_keys(user.id)
    assert any(k.name == "ci" for k in keys)
    key_id = next(k.id for k in keys if k.name == "ci")
    assert await service.revoke_api_key(user.id, key_id)
    assert await service.verify_api_key(plain) is None


async def test_job_history_is_paginated_with_an_ownership_scoped_total(user):
    other = await service.upsert_github_user(
        github_id=-int(uuid.uuid4().int % 1_000_000_000) - 2,
        login=f"other-{uuid.uuid4().hex[:8]}",
        avatar_url=None,
    )
    # Future timestamps make these deterministic page leaders without depending on the
    # pre-existing development database history.
    base = datetime.now(UTC) + timedelta(days=3650)
    owned_old = Job(
        repo_url="pagination-owned-old", migration_recipe="flask_to_fastapi",
        status="done", config={}, user_id=user.id, created_at=base,
    )
    owned_new = Job(
        repo_url="pagination-owned-new", migration_recipe="flask_to_fastapi",
        status="done", config={}, user_id=user.id, created_at=base + timedelta(seconds=1),
    )
    other_job = Job(
        repo_url="pagination-other", migration_recipe="flask_to_fastapi",
        status="done", config={}, user_id=other.id, created_at=base + timedelta(seconds=2),
    )
    async with AsyncSessionLocal() as session, session.begin():
        session.add_all([owned_old, owned_new, other_job])
    ids = [owned_old.id, owned_new.id, other_job.id]

    try:
        response = Response()
        first = await list_jobs(response, limit=1, offset=0, user=user)
        assert response.headers["X-Total-Count"] == "2"
        assert response.headers["X-Page-Limit"] == "1"
        assert response.headers["X-Page-Offset"] == "0"
        assert [job.id for job in first] == [owned_new.id]

        response = Response()
        second = await list_jobs(response, limit=1, offset=1, user=user)
        assert [job.id for job in second] == [owned_old.id]

        response = Response()
        admin_page = await list_jobs(
            response, limit=3, offset=0,
            user=SimpleNamespace(id=uuid.uuid4(), role="admin"),
        )
        assert int(response.headers["X-Total-Count"]) >= 3
        assert {job.id for job in admin_page} == set(ids)
    finally:
        async with AsyncSessionLocal() as session, session.begin():
            await session.execute(delete(Job).where(Job.id.in_(ids)))
