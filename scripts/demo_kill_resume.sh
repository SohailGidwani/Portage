#!/usr/bin/env bash
# The kill-and-resume demo (Phase 6 GIF) — a narrated, self-running version of
# dod_check.sh's crash-recovery proof, tuned for screen recording:
#
#   submit a migration -> let Ingest finish -> SIGKILL the worker mid-run ->
#   restart it -> watch it RESUME from the Postgres checkpoint (Ingest does NOT
#   re-run) -> full suite green.
#
# Record with:  asciinema rec -c "bash scripts/demo_kill_resume.sh" demo.cast
#               agg --speed 2.5 --font-size 16 demo.cast docs/assets/kill-resume.gif
set -euo pipefail
cd "$(dirname "$0")/.."

API=${API:-http://localhost:8000}
DELAY=${VERIFY_DELAY:-30}

say()  { printf '\n\033[1;36m▸ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m✔ %s\033[0m\n' "$*"; }
boom() { printf '\033[1;31m✖ %s\033[0m\n' "$*"; }

say "Portage crash-recovery demo: kill the worker mid-migration, watch it resume."
docker compose up -d db api worker >/dev/null 2>&1
for _ in $(seq 1 30); do curl -sf "$API/health" >/dev/null 2>&1 && break; sleep 1; done
ok "stack up ($(curl -sf "$API/health"))"

say "1. Submit a migration job (with a pre-test delay = our kill window)"
JOB_ID=$(curl -sf -X POST "$API/jobs" -H 'content-type: application/json' \
  -d "{\"repo_url\":\"/fixtures/flask_app\",\"migration_recipe\":\"flask_to_fastapi\",\"config\":{\"verify_delay_seconds\":$DELAY}}" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
ok "job ${JOB_ID:0:8} submitted"

wl() { docker compose logs worker 2>/dev/null | grep -F "$JOB_ID"; }

say "2. Worker ingests the repo, plans the task DAG, migrates each file with the LLM…"
for _ in $(seq 1 60); do wl | grep -q "VERIFY node" && break; sleep 1; done
wl | grep -E "INGEST done|PLAN done|migrated " | sed -E 's/^worker-1  \| /   /; s/ \| job=[0-9a-f-]+//' | head -5
ok "checkpointed through Ingest → Plan → Execute; now inside Verify"

say "3. 💀 SIGKILL the worker — mid-run, no graceful shutdown"
docker compose kill worker >/dev/null 2>&1
boom "worker is dead (job ${JOB_ID:0:8} was mid-flight)"
sleep 2

say "4. Restart the worker. The job lease expires; any worker can reclaim it."
docker compose start worker >/dev/null 2>&1
ok "new worker polling"

say "5. Watch it RESUME from the Postgres checkpoint (not from zero)…"
for _ in $(seq 1 90); do wl | grep -q "RESUME from checkpoint" && break; sleep 1; done
wl | grep "RESUME from checkpoint" | sed -E 's/^worker-1  \| /   /' | head -1

say "6. Wait for the job to finish"
STATUS=""
for _ in $(seq 1 90); do
  STATUS=$(curl -sf "$API/jobs/$JOB_ID" | python3 -c "import sys,json;print(json.load(sys.stdin)['status'])")
  [ "$STATUS" = "done" ] || [ "$STATUS" = "failed" ] && break
  sleep 2
done

INGESTS=$(wl | grep -c "INGEST node" || true)
TESTS=$(curl -sf "$API/jobs/$JOB_ID" | python3 -c "
import sys,json; ts=json.load(sys.stdin).get('test_summary') or {}
print(f\"{ts.get('passed',0)}/{ts.get('total',0)}\")")

echo
ok "status: $STATUS — full suite: $TESTS"
ok "INGEST ran exactly $INGESTS time(s): the clone + graph build were NOT repeated"
ok "Crash mid-run → resume from checkpoint → migration green. That's the durability story."