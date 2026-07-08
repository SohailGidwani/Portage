# Using Portage — CLI & MCP, every scenario

Portage is one engine with two front doors: the **CLI** (you drive an autonomous
migration) and the **MCP server** (another AI agent drives Portage's verified primitives).
This document walks through both, scenario by scenario, with real commands and real
output shapes. The dashboard (<http://localhost:3000>) mirrors everything read-only.

---

## 0. One-time setup

```bash
cp .env.example .env          # add LLM creds — see the comments in the file
docker compose --profile tools build sandbox   # the network-off test-runner image
docker compose up -d          # db -> api (runs migrations) -> worker -> frontend
cd apps/backend && uv sync    # installs the `portage` console script
```

Sanity check:

```bash
curl -s localhost:8000/health          # {"status":"ok","db":"ok"}
uv run portage --help
```

Everything below assumes you're in `apps/backend` (or replace `uv run portage` with
`uv run --project apps/backend portage` from the repo root). Point the CLI at a non-local
control plane with `PORTAGE_API=http://host:8000` or `--api`.

---

## 1. CLI — autonomous migrations

### 1.1 Migrate the bundled fixture (the 30-second demo)

```bash
uv run portage migrate /fixtures/flask_app --recipe flask_to_fastapi --watch
```

What you'll see — live task transitions, then the verdict:

```
submitted 32a69b0f-…  (flask_to_fastapi on /fixtures/flask_app)
  running  src/flaskapp/api.py (attempt 1)
  done     src/flaskapp/api.py (attempt 1)
  running  src/flaskapp/app.py (attempt 1)
  done     src/flaskapp/app.py (attempt 1)
  running  tests/conftest.py (attempt 1)
  done     tests/conftest.py (attempt 1)

job      : 32a69b0f-…
status   : done
tests    : 6/6
tasks    : 3/3 done
verdict  : GREEN — migrated, full suite passing
```

Paths like `/fixtures/flask_app` are resolved *inside the worker container* (the compose
file mounts `apps/backend/tests/fixtures` at `/fixtures`).

### 1.2 Migrate a real GitHub repo, pinned to a commit

Always pin `--ref` for reproducible runs (the eval corpus rule):

```bash
uv run portage migrate https://github.com/markdouthwaite/minimal-flask-api \
  --ref 91ae6abe493bef44fb21e4b9c34e8e94d9d2eae9 --watch
```

### 1.3 The app lives in a subdirectory of a bigger repo

```bash
uv run portage migrate https://github.com/pallets/flask \
  --ref 36e4a824f340fdee7ed50937ba8e7f6bc7d17f81 \
  --subdir examples/tutorial --watch
```

Ingest lifts `examples/tutorial` out as the workspace root and snapshots it as a fresh
git repo — rollback/worktree machinery works exactly as for a whole-repo migration.

### 1.4 Fire-and-forget (no `--watch`)

```bash
uv run portage migrate /fixtures/flask_app
# submitted 8e98232b-…
# follow with: portage status 8e98232b-…
```

The job runs on the worker regardless; `--watch` only decides whether the CLI stays
attached. Re-attach any time with `status`.

### 1.5 Check on a job / list recent jobs

```bash
uv run portage status 8e98232b-…    # task tree + attempts + verdict
uv run portage jobs --limit 10      # one line per job: id, status, recipe, tests, repo
```

`status` on a *running* job exits 0 and shows current task states — safe to poll.

### 1.6 Read the report and the diff

```bash
uv run portage report 8e98232b-…            # report.json minus the diff (task tree,
                                            # recovery actions, llm usage/cost, summaries)
uv run portage report 8e98232b-… --diff     # the full migration diff (git diff format)
uv run portage report 8e98232b-… --diff > migration.patch   # save it
```

The report's `recovery` block shows every classify→rollback/replan/skip action; the
`llm_usage` block shows calls, tokens, and cost in USD for this migration.

### 1.7 Exit codes — using the CLI in scripts/CI

| code | meaning |
|---|---|
| 0 | **honestly green**: every planned task completed, none rolled back, full suite passed |
| 1 | red: the job finished but the migration isn't complete-and-green |
| 2 | usage/infra: unknown job, malformed id, API unreachable |

```bash
if uv run portage migrate "$REPO" --ref "$SHA" --watch; then
  uv run portage report <id> --diff > out.patch    # ship the diff
else
  uv run portage status <id>                        # inspect what failed
fi
```

The 0/1 bar is the same one the eval harness scores — a run where recovery gave up and
rolled files back exits 1 even though the (original) suite passes. The CLI cannot be
fooled by a giving-up run.

### 1.8 Failure scenarios, what they look like

- **Red migration** (e.g. a template-tier app hitting a known limitation):
  `verdict : RED — not a complete green migration (see tasks/report)`, tasks show
  `skipped attempts=3` rows, exit 1. Post-mortem: `portage report <id>` → `recovery`
  actions carry `error_head`; skipped tasks keep their failing diff (visible in the
  dashboard task cards too).
- **API not running**: `portage: cannot reach the API at http://localhost:8000 — is
  `docker compose up` running?` — exit 2.
- **Nonexistent/malformed job id**: `portage: job not found` / `portage: invalid job id
  (422): not-a-uuid` — exit 2.
- **Recipe doesn't match the repo** (e.g. `--recipe pydantic_v1_to_v2` on a Flask app):
  the run degrades to ingest→verify→report — the repo's own tests are run and reported,
  nothing is changed. `status` shows zero file tasks; verdict RED (nothing migrated).
- **Worker killed mid-run** (or machine crash): the job lease expires, any worker
  re-claims it and resumes from the last checkpoint — already-migrated files are skipped
  by content hash. `--watch` keeps polling; no action needed.

### 1.9 Fault injection (demo/eval)

Deterministic faults are job-config flags — the REST route or dashboard can set them
(the CLI intentionally doesn't expose demo flags):

```bash
curl -X POST localhost:8000/jobs -H 'content-type: application/json' -d '{
  "repo_url": "/fixtures/flask_app",
  "migration_recipe": "flask_to_fastapi",
  "config": {"inject_fault": "bad_patch_until_escalation"}
}'
```

Faults: `bad_patch` (first attempt corrupted → rollback+retry rescues),
`bad_patch_until_escalation` (driver-tier attempts corrupted → the escalation model
rescues, measured in `attempts_log`), `drop_task` (planner misses a file → replan
repairs). Watch the recovery timeline on the job's dashboard page.

### 1.10 The eval harness (batch runs → `runs`/`metrics` tables)

```bash
docker compose run --rm worker python -m portage_agent.eval \
  --corpus /corpus/corpus.toml --k 3 --scenarios baseline,bad_patch \
  --repos flask-items-fixture,minimal-flask-api --suite my-suite
```

Results persist to Postgres (`runs` per execution, `metrics` mean±variance per cell) and
show up in the dashboard's eval panel. Corpus entries live in `corpus/corpus.toml`;
vet a new candidate with `scripts/vet_corpus_repo.sh <git-url> [ref] [test-args…]`.

---

## 2. MCP — Portage as tools for other agents

The MCP server exposes the *verified core* (the sandbox + graph the eval numbers were
measured on) so a coding agent can test its own work before touching your tree.

### 2.1 Wiring

- **Claude Code, inside this repo**: nothing to do — `.mcp.json` at the repo root
  declares the server; approve it when prompted. Tools appear as
  `mcp__portage__verify_patch_in_sandbox`, etc.
- **Claude Code, any other project**:
  `claude mcp add portage -- uv run --project /path/to/Portage/apps/backend python -m portage_agent.mcp`
- **Cursor** (`~/.cursor/mcp.json`):

```json
{ "mcpServers": { "portage": {
    "command": "uv",
    "args": ["run", "--project", "/path/to/Portage/apps/backend",
             "python", "-m", "portage_agent.mcp"] } } }
```

Prerequisites on the host: Docker running + the sandbox image built
(`docker compose --profile tools build sandbox`); for the graph tools,
`uv tool install code-review-graph`. The compose stack does NOT need to be up — the MCP
server is standalone.

### 2.2 `verify_patch_in_sandbox` — test a change before writing it

The contract: your repo is **copied**; the diff is applied to the copy; the copy's tests
run under `--network none`; your tree is never modified.

Scenario A — *is this repo's suite green as-is?* (empty diff):

```json
{"repo_path": "/abs/path/to/repo"}
→ {"ok": true, "applied": false, "passed": true,
   "tests": {"total": 6, "passed": 6, "failed": 0, "errors": 0, "skipped": 0}, …}
```

Scenario B — *does my proposed change keep the tests green?*

```json
{"repo_path": "/abs/path/to/repo", "diff": "<unified diff, e.g. from `git diff`>"}
→ {"ok": true, "applied": true, "passed": true, "tests": {…}}
```

Scenario C — *my change breaks something* (this is the tool earning its keep):

```json
→ {"ok": true, "applied": true, "passed": false,
   "tests": {"total": 6, "passed": 3, "failed": 3, …},
   "failing": ["tests.test_api::test_create_and_fetch_item", …],
   "output_tail": "…pytest output…"}
```

The agent reads `failing` + `output_tail`, fixes its diff, and calls again — same loop
Portage's own Execute/Verify runs.

Scenario D — *the diff is malformed / doesn't apply*:

```json
→ {"ok": false, "error": "diff does not apply", "detail": "…git apply output…"}
```

Scenario E — *scope the test run* (big repo, fast feedback):

```json
{"repo_path": "…", "diff": "…", "test_args": ["tests/test_api.py"], "timeout_seconds": 120}
```

Scenario F — *sandbox image missing*: `{"ok": false, "error": "no test report produced …
is the portage-sandbox image built? (docker compose --profile tools build sandbox)"}`.

Caveat (same rule as the eval corpus): the sandbox is offline — the repo's test deps must
already be in the sandbox image. The baked set is listed in
`apps/backend/sandbox/Dockerfile.sandbox`.

### 2.3 `repo_graph` — the structural map

```json
{"repo_path": "/abs/path/to/repo"}     // must be a git root (.git present)
→ {"ok": true, "build": "full", "files_parsed": 6, "total_nodes": 32, "total_edges": 128}
```

First call does a full build (persisted under `.code-review-graph/` in the repo); later
calls refresh incrementally (`"build": "incremental"`, counts reflect *changes*, `ok`
still true). Errors are self-explanatory: no `.git` → "pass the repository root or git
init"; CRG missing → the install command.

### 2.4 `blast_radius` — what does this change affect?

```json
{"repo_path": "/abs/path/to/repo", "changed_files": ["src/flaskapp/store.py"]}
→ {"status": "ok", …impacted files/callers/tests…}
```

Use it before editing (which tests should I run? what else depends on this?) — the same
query Portage's Plan node uses to scope Verify.

### 2.5 The intended agent workflow (what the DoD demonstrates)

1. Agent is asked to change code in repo R.
2. `repo_graph(R)` once, then `blast_radius(R, [files-to-edit])` → knows the impact set.
3. Agent drafts a diff — **without writing it**.
4. `verify_patch_in_sandbox(R, diff, test_args=<affected tests>)`.
5. Green → write the change to disk. Red → iterate on `failing`/`output_tail`, goto 4.

---

## 3. Quick reference

| I want to… | Do this |
|---|---|
| Migrate a repo and watch | `portage migrate <repo> [--ref SHA] [--subdir D] --watch` |
| Script/CI gate on the result | exit code: 0 green / 1 red / 2 infra |
| See what changed | `portage report <id> --diff` |
| Understand a failure | `portage status <id>`, then `report` → `recovery`, dashboard task cards |
| Batch-evaluate with stats | `python -m portage_agent.eval --corpus /corpus/corpus.toml --k 3 …` |
| Let my AI assistant test its edits | MCP `verify_patch_in_sandbox` (config §2.1) |
| Ask "what breaks if I touch X?" | MCP `blast_radius` |
| Demo crash recovery | `scripts/dod_check.sh` (kill mid-run → resume) |
| Prove the recovery story | `scripts/phase3_check.sh` (3 injected faults survived) |
