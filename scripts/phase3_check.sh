#!/usr/bin/env bash
# Phase 3 Definition-of-Done check: INJECTED FAULTS SURVIVED.
#
# Three deterministic fault scenarios, each submitted as a normal job with a config-gated
# fault, each required to end green (full suite) WITH the expected recovery evidence:
#
#   1. bad_patch                 — the first file's first migration attempt is corrupted
#                                  (invalid python). Recover must classify the crash,
#                                  roll back that file, and a driver-tier retry rescues it.
#   2. bad_patch_until_escalation— every driver-tier attempt of the first file is corrupted;
#                                  only the escalation-tier model can rescue the task.
#                                  Proves the measured model-escalation ladder.
#   3. drop_task                 — Plan deliberately omits the first file (a planner miss).
#                                  Recover must detect framework residue in an unplanned
#                                  file and REPLAN; the appended task then migrates green.
#
# Requires LLM creds in .env (driver + escalation model strings; see .env.example).
set -euo pipefail
cd "$(dirname "$0")/.."

API=${API:-http://localhost:8000}
COMPOSE="docker compose"
REPO=${REPO:-/fixtures/flask_app}
RECIPE=${RECIPE:-flask_to_fastapi}

log() { printf '\n=== %s ===\n' "$*"; }
fail() { printf '\nPHASE 3 DoD FAILED: %s\n' "$*" >&2; exit 1; }

submit_and_wait() { # $1 = inject_fault name -> echoes JOB_ID
  local fault=$1
  local job_id
  job_id=$(curl -sf -X POST "$API/jobs" -H 'content-type: application/json' \
    -d "{\"repo_url\":\"$REPO\",\"migration_recipe\":\"$RECIPE\",\"config\":{\"inject_fault\":\"$fault\"}}" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
  local status=""
  for _ in $(seq 1 120); do
    status=$(curl -sf "$API/jobs/$job_id" | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])")
    [ "$status" = "done" ] && break
    [ "$status" = "failed" ] && fail "job $job_id (fault=$fault) ended as failed"
    sleep 3
  done
  [ "$status" = "done" ] || fail "job $job_id (fault=$fault) did not reach done (last=$status)"
  echo "$job_id"
}

assert_scenario() { # $1 job_id  $2 fault name
  python3 - "$1" "$2" <<'PY'
import json, sys, urllib.request

job_id, fault = sys.argv[1], sys.argv[2]
api = "http://localhost:8000"

def get(path):
    with urllib.request.urlopen(f"{api}{path}") as r:
        return json.load(r)

job = get(f"/jobs/{job_id}")
tasks = get(f"/jobs/{job_id}/tasks")
report = get(f"/jobs/{job_id}/report")

ts = job.get("test_summary") or {}
rec = report.get("recovery") or {}
file_tasks = [t for t in tasks if t.get("target_path")]
recipe_tasks = [
    t for t in file_tasks
    if t.get("type") != "test_compat"
    and (t.get("verify_spec") or {}).get("origin", "recipe") == "recipe"
]
first = min(recipe_tasks, key=lambda t: t["order_index"]) if recipe_tasks else None

print(f"  tests={ts.get('passed')}/{ts.get('total')}  tasks={report.get('tasks_done')}/{report.get('tasks_total')}"
      f"  recover_visits={rec.get('visits')}  escalation={rec.get('escalation_rescued')}/{rec.get('escalation_attempted')}")

# Universal: green full suite, everything migrated.
assert ts.get("total", 0) > 0 and ts.get("passed") == ts.get("total"), "full suite not green"
assert report.get("tasks_done") == report.get("tasks_total") and report.get("tasks_total", 0) > 0, \
    "not all tasks done"
assert rec.get("visits", 0) >= 1, "Recover never ran — the fault did not exercise recovery"

classifications = {a.get("classification") for a in rec.get("actions", [])}
rollback_targets = {
    path
    for action in rec.get("actions", [])
    if action.get("action") == "rollback_regenerate"
    for path in action.get("targets", [])
}

def assert_only_attributed_batch_retried(expected_attempts):
    assert first and first["target_path"] in rollback_targets, \
        f"faulted recipe task absent from rollback attribution: {rollback_targets}"
    for task in file_tasks:
        expected = expected_attempts if task["target_path"] in rollback_targets else 1
        assert task["attempts"] == expected, \
            f"unexpected attempts for {task['target_path']}: {task['attempts']} != {expected}; " \
            f"attributed batch={sorted(rollback_targets)}"

if fault == "bad_patch":
    assert any((item or "").startswith("crash") for item in classifications), \
        f"crash not classified: {classifications}"
    assert first and first["attempts"] == 2, f"expected 2 attempts on first task, got {first and first['attempts']}"
    assert_only_attributed_batch_retried(2)
    actions = [a.get("action") for a in first["attempts_log"]]
    assert "rollback_regenerate" in actions, f"no rollback_regenerate in attempts_log: {actions}"
    tiers = {a.get("tier") for a in first["attempts_log"] if a.get("action") == "migrate"}
    assert tiers == {"driver"}, f"bad_patch should be rescued at driver tier, saw {tiers}"
elif fault == "bad_patch_until_escalation":
    assert any((item or "").startswith("crash") for item in classifications), \
        f"crash not classified: {classifications}"
    expected_escalations = len(rollback_targets)
    assert rec.get("escalation_attempted") == expected_escalations \
        and rec.get("escalation_rescued") == expected_escalations, \
        f"expected the attributed batch escalated+rescued, got " \
        f"{rec.get('escalation_rescued')}/{rec.get('escalation_attempted')} " \
        f"for {sorted(rollback_targets)}"
    assert first and first["attempts"] == 3, f"expected 3 attempts, got {first and first['attempts']}"
    assert_only_attributed_batch_retried(3)
    last_migrate = [a for a in first["attempts_log"] if a.get("action") == "migrate"][-1]
    assert last_migrate.get("tier") == "escalation", f"final attempt not escalation-tier: {last_migrate}"
elif fault == "drop_task":
    assert "unplanned_residue" in classifications, f"no replan classification: {classifications}"
    adapters = [t for t in file_tasks if t.get("type") == "test_compat"]
    assert adapters, "deterministic compatibility infrastructure was not preserved"
    replans = [a for a in rec.get("actions", []) if a.get("action") == "replan"]
    assert replans and all(
        target != adapters[0]["target_path"]
        for action in replans for target in action.get("targets", [])
    ), f"replan targeted infrastructure instead of a recipe file: {replans}"

print(f"  scenario '{fault}' assertions passed")
PY
}

log "ensure sandbox image is built + stack is up (rebuilt images assumed)"
$COMPOSE --profile tools build sandbox >/dev/null
$COMPOSE up -d db api worker >/dev/null
for _ in $(seq 1 30); do curl -sf "$API/health" >/dev/null 2>&1 && break; sleep 1; done
curl -sf "$API/health" >/dev/null || fail "api /health never came up"

log "scenario 1/3: bad_patch (rollback + regenerate rescues)"
J1=$(submit_and_wait bad_patch)
echo "JOB=$J1"
assert_scenario "$J1" bad_patch

log "scenario 2/3: bad_patch_until_escalation (model escalation rescues)"
J2=$(submit_and_wait bad_patch_until_escalation)
echo "JOB=$J2"
assert_scenario "$J2" bad_patch_until_escalation

log "scenario 3/3: drop_task (replan repairs a planner miss)"
J3=$(submit_and_wait drop_task)
echo "JOB=$J3"
assert_scenario "$J3" drop_task

printf '\nPHASE 3 DoD PASSED: all injected faults survived (rollback+retry, escalation, replan).\n'
