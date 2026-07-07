#!/usr/bin/env bash
# Phase 4 harness smoke — proves the eval machinery end-to-end on the corpus-of-1.
#
# Runs the harness (baseline + bad_patch, K=2 → 4 real LLM-driven migrations) against the
# bundled fixture, then asserts the contract: `runs` rows with real outcomes and
# `metrics` rows with mean±variance, including cost-per-migration > 0.
#
# This is NOT the Phase 4 DoD (that needs ≥10 corpus repos) — it's the proof that the
# harness → runs/metrics pipeline works, so corpus curation is the only thing left
# between here and the DoD.
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE="docker compose"
K=${K:-2}
SCENARIOS=${SCENARIOS:-baseline,bad_patch}
SUITE="smoke-$(date +%Y%m%d-%H%M%S)"

log() { printf '\n=== %s ===\n' "$*"; }
fail() { printf '\nPHASE 4 SMOKE FAILED: %s\n' "$*" >&2; exit 1; }

log "stack up (worker must be polling; harness runs alongside it)"
$COMPOSE --profile tools build sandbox >/dev/null
$COMPOSE up -d db api worker >/dev/null
for _ in $(seq 1 30); do curl -sf localhost:8000/health >/dev/null 2>&1 && break; sleep 1; done
curl -sf localhost:8000/health >/dev/null || fail "api /health never came up"

log "run harness: suite=$SUITE k=$K scenarios=$SCENARIOS (fixture only — this smokes the
     harness CONTRACT; the full corpus grid is a deliberate, separate run)"
$COMPOSE run --rm worker python -m portage_agent.eval \
  --corpus /corpus/corpus.toml --k "$K" --scenarios "$SCENARIOS" --suite "$SUITE" \
  --repos flask-items-fixture

log "assert the runs/metrics contract"
$COMPOSE exec -T db psql -U portage -d portage -tA -c "
  SELECT status, count(*) FROM runs WHERE suite='$SUITE' GROUP BY status ORDER BY status;
" | tee /tmp/portage_smoke_runs.txt

python3 - "$SUITE" <<'PY'
import subprocess, sys
suite = sys.argv[1]

def q(sql: str) -> list[list[str]]:
    out = subprocess.run(
        ["docker", "compose", "exec", "-T", "db", "psql", "-U", "portage", "-d", "portage",
         "-tA", "-F", "|", "-c", sql],
        capture_output=True, text=True, check=True).stdout.strip()
    return [line.split("|") for line in out.splitlines() if line]

runs = q(f"SELECT scenario, k_index, status, tests_passed, tests_total, cost_usd, "
         f"recover_visits FROM runs WHERE suite='{suite}' ORDER BY scenario, k_index")
print("runs:")
for r in runs:
    print("  ", r)
n_expected = len("BASELINE BAD_PATCH".split()) * 2  # scenarios x K (matches script defaults)
assert len(runs) == n_expected, f"expected {n_expected} runs, got {len(runs)}"
assert all(r[2] == "green" for r in runs), "not every smoke run ended green"
assert all(float(r[5]) > 0 for r in runs), "cost_usd not tracked (is the model priceable?)"
bad_patch = [r for r in runs if r[0] == "bad_patch"]
assert all(int(r[6]) >= 1 for r in bad_patch), "bad_patch runs recorded no recover visits"

metrics = q(f"SELECT scenario, metric, k, mean, variance FROM metrics WHERE suite='{suite}' "
            f"ORDER BY scenario, metric")
print("metrics rows:", len(metrics))
for m in metrics:
    print("  ", m)
assert len(metrics) == 2 * 8, f"expected 16 metric rows (2 scenarios x 8 metrics), got {len(metrics)}"
green = {(m[0]): float(m[3]) for m in metrics if m[1] == "suite_green"}
assert green.get("baseline") == 1.0 and green.get("bad_patch") == 1.0, \
    f"suite_green means wrong: {green}"
cost = [m for m in metrics if m[1] == "cost_usd"]
assert all(float(m[3]) > 0 for m in cost), "cost_usd metric not aggregated"
print("\ncontract assertions passed")
PY

printf '\nPHASE 4 SMOKE PASSED: harness -> runs/metrics contract works end-to-end.\n'
