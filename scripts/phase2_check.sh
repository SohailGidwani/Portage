#!/usr/bin/env bash
# Phase 2 functional Definition-of-Done check.
#
# Proves the autonomous Flask→FastAPI migration end-to-end on the bundled fixture:
#   submit a `flask_to_fastapi` job -> Ingest → Plan → Execute (LLM) → Verify → Integrate →
#   Report, all checkpointed -> the migrated app's FULL test suite passes.
#
# Requires LLM creds in .env (e.g. Azure OpenAI: LLM_DRIVER_MODEL=azure/<deployment> +
# AZURE_API_*). The Execute node runs in the worker (which has network); the test sandbox
# stays --network none.
set -euo pipefail
cd "$(dirname "$0")/.."

API=${API:-http://localhost:8000}
COMPOSE="docker compose"
RECIPE=${RECIPE:-flask_to_fastapi}
REPO=${REPO:-/fixtures/flask_app}

log() { printf '\n=== %s ===\n' "$*"; }
fail() { printf '\nPHASE 2 DoD FAILED: %s\n' "$*" >&2; exit 1; }

log "ensure sandbox image is built (tools profile) + stack is up"
$COMPOSE --profile tools build sandbox >/dev/null
$COMPOSE up -d db api worker >/dev/null
for _ in $(seq 1 30); do curl -sf "$API/health" >/dev/null 2>&1 && break; sleep 1; done
curl -sf "$API/health" >/dev/null || fail "api /health never came up"

# Heads-up if no provider creds look present in the worker env (best-effort, non-fatal).
if ! $COMPOSE exec -T worker sh -c 'env' 2>/dev/null \
     | grep -qE 'AZURE_API_KEY=.|ANTHROPIC_API_KEY=.|GEMINI_API_KEY=.|AWS_ACCESS_KEY_ID=.'; then
  echo "WARN: no LLM provider creds visible in the worker env — the migration will fail."
  echo "      Add them to .env and re-run: docker compose up -d --force-recreate worker"
fi

log "submit a Flask→FastAPI migration job for the fixture app"
JOB_ID=$(curl -sf -X POST "$API/jobs" -H 'content-type: application/json' \
  -d "{\"repo_url\":\"$REPO\",\"migration_recipe\":\"$RECIPE\"}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "JOB_ID=$JOB_ID"

log "wait for done (LLM-driven migration — allow time)"
STATUS=""
for _ in $(seq 1 90); do
  STATUS=$(curl -sf "$API/jobs/$JOB_ID" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
  [ "$STATUS" = "done" ] && break
  [ "$STATUS" = "failed" ] && fail "job ended as failed (see: docker compose logs worker)"
  sleep 3
done
[ "$STATUS" = "done" ] || fail "job did not reach done (last=$STATUS)"

log "assert the migration result on the job row"
JOB_JSON=$(mktemp)
curl -sf "$API/jobs/$JOB_ID" -o "$JOB_JSON"
python3 - "$JOB_JSON" <<'PY'
import json, sys
job = json.load(open(sys.argv[1]))
ts = job.get("test_summary") or {}
print("final test_summary:", {k: ts.get(k) for k in ("total","passed","failed","errors")})
assert ts.get("total", 0) > 0, "no tests in final (full-suite) report"
assert ts.get("passed") == ts.get("total"), "not all tests passed post-migration"
assert ts.get("failed", 0) == 0 and ts.get("errors", 0) == 0, "failures/errors post-migration"
print("full-suite assertion passed")
PY

log "assert the structured migration report (migrated, tasks done, diff is real FastAPI)"
RP=$(python3 -c "import json;print(json.load(open('$JOB_JSON'))['report_path'])")
REPORT=$(mktemp)
docker run --rm -v "${WORKSPACES_VOLUME:-portage_workspaces}:/workspaces" alpine cat "$RP" > "$REPORT"
python3 - "$REPORT" <<'PY'
import json, sys
rep = json.load(open(sys.argv[1]))
print("migrated     :", rep.get("migrated"))
print("tasks_done   :", rep.get("tasks_done"), "/", rep.get("tasks_total"))
print("affected_tests:", rep.get("affected_tests"))
isum = rep.get("integrate_summary") or {}
print("integrate    :", {k: isum.get(k) for k in ("total","passed","failed","errors")})
diff = rep.get("diff") or ""
assert rep.get("migrated") is True, "report says not migrated"
assert rep.get("tasks_total", 0) > 0 and rep.get("tasks_done") == rep.get("tasks_total"), \
    "not all migration tasks completed"
assert "fastapi" in diff.lower(), "diff does not introduce FastAPI"
assert ("flask" in diff.lower() and "-" in diff), "diff does not remove Flask"
print("\n--- migration diff (first 60 lines) ---")
print("\n".join(diff.splitlines()[:60]))
print("\nmigration-report assertion passed")
PY

printf '\nPHASE 2 DoD PASSED: Flask app autonomously migrated to FastAPI; full suite green.\n'
