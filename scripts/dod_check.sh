#!/usr/bin/env bash
# Crash-recovery Definition-of-Done check (repeatable) — now against the real Phase 1
# Ingest -> Verify -> Report graph.
#
# Proves: kill the worker mid-Verify, restart it, and it RESUMES from the post-Ingest
# checkpoint — the expensive Ingest (clone + build the code graph) does NOT re-run.
#
# Strategy: submit a job with a Verify pre-test delay (a deterministic kill window AFTER
# Ingest has checkpointed); wait until the worker is inside Verify; SIGKILL the worker
# (`docker compose kill`, not a graceful stop); restart it; wait for done; then assert
# against the worker logs filtered to this job's UUID:
#   * exactly ONE "INGEST node" line ever   (Ingest ran once; resume did NOT re-ingest)
#   * a "RESUME from checkpoint" line        (the restart picked up the checkpoint)
#   * final job status == done with tests passed
set -euo pipefail
cd "$(dirname "$0")/.."

API=${API:-http://localhost:8000}
COMPOSE="docker compose"
DELAY=${VERIFY_DELAY:-30}

log() { printf '\n=== %s ===\n' "$*"; }
fail() { printf '\nDoD FAILED: %s\n' "$*" >&2; exit 1; }

log "ensure sandbox image built + db, api, worker up"
$COMPOSE --profile tools build sandbox >/dev/null
$COMPOSE up -d db api worker >/dev/null
for _ in $(seq 1 30); do curl -sf "$API/health" >/dev/null 2>&1 && break; sleep 1; done
curl -sf "$API/health" >/dev/null || fail "api /health never came up"

log "submit a job with a Verify delay (kill window after Ingest)"
JOB_ID=$(curl -sf -X POST "$API/jobs" -H 'Content-Type: application/json' \
  -d "{\"repo_url\":\"/fixtures/sample_repo\",\"migration_recipe\":\"pydantic_v1_to_v2\",\"config\":{\"verify_delay_seconds\":$DELAY}}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "JOB_ID=$JOB_ID (verify_delay=${DELAY}s)"

worker_logs() { $COMPOSE logs worker 2>/dev/null | grep -F "$JOB_ID" || true; }

log "wait until the worker is inside Verify (Ingest has checkpointed)"
for _ in $(seq 1 40); do
  worker_logs | grep -q "VERIFY node" && break
  sleep 1
done
worker_logs | grep -q "VERIFY node" || fail "worker never reached Verify"
worker_logs | grep -q "INGEST done" || fail "Ingest did not complete before Verify"
echo "ingest completed; worker now in Verify delay"

log "SIGKILL the worker mid-Verify (crash, not graceful stop)"
$COMPOSE kill worker >/dev/null
sleep 1

log "restart the worker"
$COMPOSE start worker >/dev/null

log "wait for the job to reach done (lease expires, then Verify re-runs)"
STATUS=""
for _ in $(seq 1 90); do
  STATUS=$(curl -sf "$API/jobs/$JOB_ID" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
  [ "$STATUS" = "done" ] && break
  [ "$STATUS" = "failed" ] && fail "job ended as failed"
  sleep 2
done
[ "$STATUS" = "done" ] || fail "job did not reach done (last status=$STATUS)"

log "assert resume semantics from the logs"
INGEST_COUNT=$(worker_logs | grep -c "INGEST node" || true)
RESUME_LINE=$(worker_logs | grep "RESUME from checkpoint" | head -1 || true)
PASSED=$(curl -sf "$API/jobs/$JOB_ID" \
  | python3 -c "import sys,json; print((json.load(sys.stdin).get('test_summary') or {}).get('passed',0))")

echo "INGEST node occurrences : $INGEST_COUNT (expected 1)"
echo "RESUME line             : ${RESUME_LINE:-<none>}"
echo "tests passed            : $PASSED"

[ "$INGEST_COUNT" = "1" ] || fail "Ingest ran $INGEST_COUNT times (expected 1 -> it re-ingested instead of resuming)"
[ -n "$RESUME_LINE" ]     || fail "no RESUME line (worker did not resume from checkpoint)"
[ "${PASSED:-0}" -gt 0 ]  || fail "no tests passed after resume"

printf '\nDoD PASSED: killed mid-Verify, resumed from checkpoint — Ingest ran once (clone+build NOT repeated), tests green.\n'
