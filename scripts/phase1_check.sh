#!/usr/bin/env bash
# Phase 1 functional Definition-of-Done check.
#
# Proves: given a repo, the Ingest -> Verify -> Report pipeline produces BOTH
#   (1) a structured test report (pass/fail counts), AND
#   (2) a queryable structural graph (node/edge counts).
# Runs the controlled fixture repo through compose and asserts on the job's result columns.
set -euo pipefail
cd "$(dirname "$0")/.."

API=${API:-http://localhost:8000}
COMPOSE="docker compose"

log() { printf '\n=== %s ===\n' "$*"; }
fail() { printf '\nPHASE 1 DoD FAILED: %s\n' "$*" >&2; exit 1; }

log "ensure sandbox image is built (tools profile) + stack is up"
$COMPOSE --profile tools build sandbox >/dev/null
$COMPOSE up -d db api worker >/dev/null
for _ in $(seq 1 30); do curl -sf "$API/health" >/dev/null 2>&1 && break; sleep 1; done
curl -sf "$API/health" >/dev/null || fail "api /health never came up"

log "submit a job for the fixture repo"
JOB_ID=$(curl -sf -X POST "$API/jobs" -H 'content-type: application/json' \
  -d '{"repo_url":"/fixtures/sample_repo","migration_recipe":"pydantic_v1_to_v2"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "JOB_ID=$JOB_ID"

log "wait for done"
STATUS=""
for _ in $(seq 1 45); do
  STATUS=$(curl -sf "$API/jobs/$JOB_ID" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
  [ "$STATUS" = "done" ] && break
  [ "$STATUS" = "failed" ] && fail "job ended as failed"
  sleep 2
done
[ "$STATUS" = "done" ] || fail "job did not reach done (last=$STATUS)"

log "assert the two DoD artifacts on the job result"
JOB_JSON=$(mktemp)
curl -sf "$API/jobs/$JOB_ID" -o "$JOB_JSON"
python3 - "$JOB_JSON" <<'PY'
import json, sys
job = json.load(open(sys.argv[1]))
ts = job.get("test_summary") or {}
gs = job.get("graph_summary") or {}
print("test_summary :", {k: ts.get(k) for k in ("total","passed","failed","errors")})
print("graph_summary:", {k: gs.get(k) for k in ("files_parsed","total_nodes","total_edges")})
print("report_path  :", job.get("report_path"))

# (1) structured test report: tests ran, all passed
assert ts.get("total", 0) > 0, "no tests in report"
assert ts.get("passed", 0) == ts.get("total"), "not all tests passed"
assert ts.get("failed", 0) == 0 and ts.get("errors", 0) == 0, "failures/errors present"
# (2) queryable graph: nodes + edges built
assert gs.get("total_nodes", 0) > 0, "graph has no nodes"
assert gs.get("total_edges", 0) > 0, "graph has no edges"
# report persisted
assert job.get("report_path"), "no report_path persisted"
print("\nbuild + test assertions passed")
PY

log "assert the graph was actually QUERIED (blast-radius via MCP) in report.json"
RP=$(python3 -c "import json;print(json.load(open('$JOB_JSON'))['report_path'])")
REPORT=$(mktemp)
docker run --rm -v "${WORKSPACES_VOLUME:-portage_workspaces}:/workspaces" alpine cat "$RP" > "$REPORT"
python3 - "$REPORT" <<'PY'
import json, sys
rep = json.load(open(sys.argv[1]))
blast = rep.get("blast_radius_sample") or {}
print("blast_radius_sample.status:", blast.get("status"))
assert blast.get("status") == "ok", f"blast-radius query did not return ok: {blast}"
print("graph-query assertion passed")
PY

printf '\nPHASE 1 DoD PASSED: repo -> structured test report + queried structural graph.\n'
