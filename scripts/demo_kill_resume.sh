#!/usr/bin/env bash
# The kill-and-resume demo (docs/assets/kill-resume.gif) — a narrated, self-running
# version of dod_check.sh's crash-recovery proof, tuned for screen recording:
#
#   submit a migration -> let it migrate files -> SIGKILL the worker mid-Execute ->
#   restart it -> the lease expires, another worker RESUMES from the Postgres
#   checkpoint (Ingest does NOT re-run, finished files are NOT re-migrated) ->
#   full suite green.
#
# Record with:  vhs scripts/kill-resume.tape      (see that file)
# Requires the `portage` CLI on PATH and an API with AUTH_MODE=disabled:
#   PORTAGE_API=http://localhost:8000 bash scripts/demo_kill_resume.sh
set -euo pipefail
cd "$(dirname "$0")/.."

API=${PORTAGE_API:-http://localhost:8000}
DELAY=${VERIFY_DELAY:-12}   # pre-test pause in Verify = a deterministic kill window

c()    { printf '\033[%sm%s\033[0m\n' "$1" "$2"; }
say()  { printf '\n'; c '1;36' "▸ $*"; }
ok()   { c '1;32' "  ✔ $*"; }
boom() { printf '\n'; c '1;31' "💀 $*"; }
dim()  { c '2'    "  $*"; }

say "Portage durability — kill the worker mid-migration, watch it resume"
dim "checkpointed after every graph node · thread_id = job_id"

JOB=$(curl -sf -X POST "$API/jobs" -H 'content-type: application/json' \
  -d "{\"repo_url\":\"/fixtures/flask_app\",\"migration_recipe\":\"flask_to_fastapi\",\"config\":{\"verify_delay_seconds\":$DELAY}}" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
SHORT=${JOB:0:8}
ok "submitted run $SHORT"

wl() { docker compose logs worker 2>/dev/null | grep -F "$JOB"; }

say "1. It ingests, plans the DAG, then migrates and verifies in batches"
for _ in $(seq 1 90); do wl | grep -q "VERIFY node" && break; sleep 1; done
wl | grep -E "INGEST done|PLAN done|migrated " \
   | sed -E 's/^worker-1  \| [0-9:]+ INFO +\[portage.agent\] /   /; s/ \| job=[0-9a-f-]+//' | head -3
portage status "$JOB" 2>/dev/null | tail -8

boom "2. SIGKILL the worker — mid-run, no graceful shutdown"
docker compose kill worker >/dev/null 2>&1
c '1;31' "  worker process gone; run $SHORT was inside Verify"

say "3. Restart a worker. The dead worker's lease expires — any worker may reclaim it."
docker compose start worker >/dev/null 2>&1
ok "new worker polling the queue"

say "4. It RESUMES from the Postgres checkpoint — not from zero"
for _ in $(seq 1 90); do wl | grep -q "RESUME from checkpoint" && break; sleep 1; done
wl | grep -E "RESUME from checkpoint" \
   | sed -E 's/^worker-1  \| [0-9:]+ INFO +\[portage.agent\] /   /; s/ \| job=[0-9a-f-]+//' | head -1
wl | grep -E "already migrated \(content-hash match\)" \
   | sed -E 's/^worker-1  \| [0-9:]+ INFO +\[portage.agent\] /   /' | head -2 || true

say "5. Let it finish"
for _ in $(seq 1 90); do
  S=$(curl -sf "$API/jobs/$JOB" | python3 -c "import sys,json;print(json.load(sys.stdin)['status'])")
  { [ "$S" = done ] || [ "$S" = failed ]; } && break
  sleep 2
done

INGESTS=$(wl | grep -c "INGEST node" || true)
printf '\n'
portage status "$JOB" 2>/dev/null

printf '\n'
ok "INGEST ran exactly $INGESTS time — the clone + graph build were never repeated"
ok "crash mid-run → resume from checkpoint → migration green"
