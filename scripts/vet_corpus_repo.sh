#!/usr/bin/env bash
# Vet one corpus candidate (corpus/README.md procedure, step 1).
#
#   scripts/vet_corpus_repo.sh <git-url> [ref] [test-args...]
#
# Clones the repo (at `ref` if given, else default-branch HEAD — and prints the SHA to
# pin), sizes it up, and runs its test suite OFFLINE in the portage-sandbox image — the
# exact runtime the eval uses. Green here = criteria 2+3 (real suite, sandbox-runnable)
# hold and the entry can go into corpus.toml with the printed SHA.
set -euo pipefail

URL=${1:?usage: vet_corpus_repo.sh <git-url> [ref] [test-args...]}
REF=${2:-}
shift $(( $# > 1 ? 2 : 1 ))
TEST_ARGS=("$@")

log() { printf '\n=== %s ===\n' "$*"; }

TMP=$(mktemp -d /tmp/portage-vet.XXXXXX)
trap 'rm -rf "$TMP"' EXIT

log "clone $URL ${REF:+@ $REF}"
if [ -n "$REF" ]; then
  git init -q "$TMP/repo"
  git -C "$TMP/repo" remote add origin "$URL"
  git -C "$TMP/repo" fetch -q --depth 1 origin "$REF"
  git -C "$TMP/repo" checkout -q --detach FETCH_HEAD
else
  git clone -q --depth 1 "$URL" "$TMP/repo"
fi
SHA=$(git -C "$TMP/repo" rev-parse HEAD)

log "shape"
cd "$TMP/repo"
PY_COUNT=$(find . -name '*.py' -not -path './.git/*' | wc -l | tr -d ' ')
TEST_COUNT=$(find . \( -name 'test_*.py' -o -name '*_test.py' -o -name 'tests.py' \) -not -path './.git/*' | wc -l | tr -d ' ')
LOC=$(find . -name '*.py' -not -path './.git/*' -exec cat {} + | wc -l | tr -d ' ')
echo "pinned SHA : $SHA"
echo "py files   : $PY_COUNT (tests: $TEST_COUNT) | LOC: $LOC"
echo "license    : $(ls LICENSE* COPYING* 2>/dev/null | head -1 || echo '<none found — check!>')"
echo "deps       :"
cat requirements*.txt Pipfile pyproject.toml setup.py 2>/dev/null \
  | grep -iE '^\s*[a-z0-9_.-]+\s*([=<>~!]|$)|install_requires|dependencies' \
  | grep -vE '^\s*#' | head -20 || echo "  <no dep manifest found>"

log "offline test run in portage-sandbox (${TEST_ARGS[*]:-whole suite})"
set +e
docker run --rm --network none -v "$TMP/repo:/repo" -w /repo \
  portage-sandbox:latest run-tests ${TEST_ARGS[@]+"${TEST_ARGS[@]}"} 2>&1 | tail -15
CODE=$?
set -e

log "verdict"
if [ -f .portage-report.xml ]; then
  python3 - <<PY
import xml.etree.ElementTree as ET
root = ET.parse(".portage-report.xml").getroot()
suites = root.findall("testsuite") if root.tag == "testsuites" else [root]
t = sum(int(s.get("tests", 0)) for s in suites)
f = sum(int(s.get("failures", 0)) + int(s.get("errors", 0)) for s in suites)
print(f"tests={t} failed/errored={f}")
print("VERDICT:", "GREEN — corpus-ready at the SHA above" if t > 0 and f == 0
      else "RED — analyze (missing dep? network use? real failure)")
PY
else
  echo "VERDICT: RED — no report produced (exit $CODE); suite crashed before pytest wrote XML"
fi
