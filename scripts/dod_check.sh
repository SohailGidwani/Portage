#!/usr/bin/env bash
# Phase 0 Definition-of-Done check (repeatable).
#
# Proves: kill the worker mid-graph, restart it, and it RESUMES from the last checkpoint
# (the `start` node does NOT re-run) rather than starting from zero.
#
# Strategy: submit a job; wait until the worker is inside the `work` node; SIGKILL the
# worker (`docker compose kill`, not a graceful stop); restart it; wait for the job to
# finish; then assert against the worker logs filtered to this job's UUID:
#   * exactly ONE "START node" line ever          (start ran once, did not re-run)
#   * a "RESUME from checkpoint" line             (the restart picked up the checkpoint)
#   * the run_marker stamped by START == the run_marker loaded on RESUME
#   * final job status == done
set -euo pipefail
cd "$(dirname "$0")/.."

API=${API:-http://localhost:8000}
COMPOSE="docker compose"

log() { printf '\n=== %s ===\n' "$*"; }
fail() { printf '\nDoD FAILED: %s\n' "$*" >&2; exit 1; }

log "ensure db, api, worker are up"
$COMPOSE up -d db api worker >/dev/null
# wait for API health
for _ in $(seq 1 30); do
  if curl -sf "$API/health" >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -sf "$API/health" >/dev/null || fail "api /health never came up"

log "submit a job"
JOB_ID=$(curl -sf -X POST "$API/jobs" -H 'Content-Type: application/json' \
  -d '{"repo_url":"https://github.com/acme/dod","migration_recipe":"pydantic_v1_to_v2"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "JOB_ID=$JOB_ID"

worker_logs() { $COMPOSE logs worker 2>/dev/null | grep -F "$JOB_ID" || true; }

log "wait until the worker is inside the work node"
for _ in $(seq 1 40); do
  if worker_logs | grep -q "WORK node BEGIN"; then break; fi
  sleep 1
done
worker_logs | grep -q "WORK node BEGIN" || fail "worker never reached the work node"
RUN1_MARKER=$(worker_logs | grep "START node" | head -1 | sed -n 's/.*run_marker=\([0-9a-f]*\).*/\1/p')
echo "run1 START run_marker=$RUN1_MARKER"
[ -n "$RUN1_MARKER" ] || fail "could not read run_marker from the START line"

log "SIGKILL the worker mid-work (crash, not graceful stop)"
$COMPOSE kill worker >/dev/null
sleep 1

log "restart the worker"
$COMPOSE start worker >/dev/null

log "wait for the job to reach done (lease must expire, then work re-runs)"
STATUS=""
for _ in $(seq 1 90); do
  STATUS=$(curl -sf "$API/jobs/$JOB_ID" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
  if [ "$STATUS" = "done" ]; then break; fi
  if [ "$STATUS" = "failed" ]; then fail "job ended as failed"; fi
  sleep 2
done
[ "$STATUS" = "done" ] || fail "job did not reach done (last status=$STATUS)"

log "assert resume semantics from the logs"
START_COUNT=$(worker_logs | grep -c "START node" || true)
RESUME_LINE=$(worker_logs | grep "RESUME from checkpoint" | head -1 || true)
RESUME_MARKER=$(printf '%s' "$RESUME_LINE" | sed -n 's/.*loaded_run_marker=\([0-9a-f]*\).*/\1/p')

echo "START node occurrences : $START_COUNT (expected 1)"
echo "RESUME line            : ${RESUME_LINE:-<none>}"
echo "run1 marker            : $RUN1_MARKER"
echo "resume loaded marker   : ${RESUME_MARKER:-<none>}"

[ "$START_COUNT" = "1" ] || fail "start node ran $START_COUNT times (expected 1 -> it restarted from zero)"
[ -n "$RESUME_LINE" ]    || fail "no RESUME line (worker did not resume from checkpoint)"
[ "$RUN1_MARKER" = "$RESUME_MARKER" ] || fail "run_marker mismatch (resumed a different/new run)"

printf '\nDoD PASSED: killed mid-work, resumed from checkpoint (start ran once, marker %s preserved).\n' "$RUN1_MARKER"
