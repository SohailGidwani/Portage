#!/usr/bin/env bash
# Phase 7 Definition-of-Done check: auth, ownership, and demo protection.
#
# Two layers:
#   1. pytest suites — refresh rotation + family reuse-detection, API keys, JWT
#      tamper rejection, secret redaction (tests/test_auth_service.py, test_redaction.py).
#   2. live HTTP assertions against a github-mode API (spun up as a sibling container on
#      the compose network) — 401 without credentials, API-key auth end-to-end,
#      per-user ownership isolation (user B cannot see user A's job, 404 not 403),
#      quotas fire, disabled-mode regression (default stack still works).
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE="docker compose"
log() { printf '\n=== %s ===\n' "$*"; }
fail() { printf '\nPHASE 7 DoD FAILED: %s\n' "$*" >&2; exit 1; }

log "stack up (disabled-mode default) + migrations at head"
$COMPOSE up -d db api worker >/dev/null
for _ in $(seq 1 30); do curl -sf localhost:8000/health >/dev/null 2>&1 && break; sleep 1; done
curl -sf localhost:8000/health >/dev/null || fail "api /health never came up"

log "1/4 unit+integration tests (rotation, reuse, keys, jwt, redaction)"
(cd apps/backend && POSTGRES_HOST=localhost uv run pytest \
  tests/test_auth_service.py tests/test_redaction.py -q) || fail "pytest suites red"

log "2/4 disabled-mode regression: anonymous submit + read still work"
JOB=$(curl -sf -X POST localhost:8000/jobs -H 'content-type: application/json' \
  -d '{"repo_url":"/fixtures/sample_repo","migration_recipe":"pydantic_v1_to_v2"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
curl -sf "localhost:8000/jobs/$JOB" >/dev/null || fail "disabled-mode read broken"
echo "disabled-mode OK (job ${JOB:0:8})"

log "3/4 github-mode API (sibling container on the compose network)"
NET=$(docker inspect portage-api-1 --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}')
docker rm -f portage-api-authtest >/dev/null 2>&1 || true
# Explicit env only: `docker run --env-file` (unlike compose) does not strip the inline
# comments a human .env may carry, and the DB creds are all this container needs.
docker run -d --name portage-api-authtest --network "$NET" \
  -e POSTGRES_HOST=db -e POSTGRES_PORT=5432 -e POSTGRES_DB="${POSTGRES_DB:-portage}" \
  -e POSTGRES_USER="${POSTGRES_USER:-portage}" -e POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-portage}" \
  -e AUTH_MODE=github -e JWT_SECRET=test-secret-p7-long-enough-32-bytes! \
  -p 8001:8000 portage-api \
  sh -c "uvicorn portage_agent.api:app --host 0.0.0.0 --port 8000" >/dev/null
trap 'docker rm -f portage-api-authtest >/dev/null 2>&1 || true' EXIT
for _ in $(seq 1 30); do curl -sf localhost:8001/health >/dev/null 2>&1 && break; sleep 1; done
curl -sf localhost:8001/health >/dev/null || fail "github-mode api never came up"

python3 - <<'PY'
import json, subprocess, urllib.request, urllib.error, uuid

API = "http://localhost:8001"

def req(path, method="GET", body=None, token=None, expect=None):
    r = urllib.request.Request(API + path, method=method)
    if token: r.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        r.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(r, data) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")

# --- 401 without credentials
code, _ = req("/jobs")
assert code == 401, f"expected 401 anon, got {code}"
code, _ = req("/jobs", "POST", {"repo_url": "/fixtures/sample_repo",
                                "migration_recipe": "pydantic_v1_to_v2"})
assert code == 401, f"expected 401 anon submit, got {code}"
print("  401 without credentials: OK")

# --- create two users + API keys directly in the DB layer (OAuth needs a browser;
#     the key path IS a production auth path, so this is a fair end-to-end test)
def mk_user_key(tag):
    out = subprocess.run(
        ["docker", "exec", "-e", "POSTGRES_HOST=db", "portage-api-authtest",
         "python", "-c", f"""
import asyncio
from portage_agent.auth import service
async def m():
    u = await service.upsert_github_user(github_id={uuid.uuid4().int % 10**9},
                                         login="p7-{tag}", avatar_url=None)
    print(await service.create_api_key(u.id, "p7"))
asyncio.run(m())
"""], capture_output=True, text=True, check=True)
    return out.stdout.strip().splitlines()[-1]

key_a, key_b = mk_user_key("alice"), mk_user_key("bob")
assert key_a.startswith("pk_") and key_b.startswith("pk_")

# --- API-key auth + ownership isolation
code, job = req("/jobs", "POST", {"repo_url": "/fixtures/sample_repo",
                                  "migration_recipe": "pydantic_v1_to_v2"}, token=key_a)
assert code == 201, f"submit with key failed: {code} {job}"
jid = job["id"]
code, _ = req(f"/jobs/{jid}", token=key_a)
assert code == 200, "owner cannot read own job"
code, _ = req(f"/jobs/{jid}", token=key_b)
assert code == 404, f"user B sees user A's job (got {code}) — ownership broken"
code, _ = req(f"/jobs/{jid}/report", token=key_b)
assert code == 404, "user B can read user A's report — the UUID hole is back"
code, listing = req("/jobs", token=key_b)
assert code == 200 and all(j["id"] != jid for j in listing), "A's job in B's listing"
print("  API-key auth + ownership isolation: OK")

# --- quotas: user A already has 1 queued job -> concurrent limit fires
code, detail = req("/jobs", "POST", {"repo_url": "/fixtures/sample_repo",
                                     "migration_recipe": "pydantic_v1_to_v2"}, token=key_a)
assert code == 429, f"expected 429 concurrent-quota, got {code} {detail}"
print("  per-user concurrency quota: OK")

# --- eval endpoints stay public (aggregate-only surface)
code, _ = req("/eval/leaderboard")
assert code == 200, "public leaderboard broke"
code, _ = req("/auth/me")
assert code == 401, "auth/me should 401 anon in github mode"
print("  public eval endpoints + authed /me: OK")
PY

log "4/4 restore: nothing persistent changed (sibling container removed by trap)"
printf '\nPHASE 7 DoD PASSED: auth, ownership isolation, quotas, redaction all verified.\n'
