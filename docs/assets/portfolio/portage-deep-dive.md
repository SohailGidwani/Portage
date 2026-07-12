# Portage: Technical Deep Dive

> Portfolio deep dive. Pair with [`portage.md`](./portage.md) for the project page. Asset paths are relative to this file (`../…`). Every number below comes from the `runs`/`metrics` tables or from documented DoD scripts — nothing hand-waved.

| | |
|---|---|
| **Source** | [github.com/SohailGidwani/Portage](https://github.com/SohailGidwani/Portage) |
| **Project page** | [portage.md](./portage.md) |
| **Live demo** | `LIVE_DEMO_URL` *(fill after deploy)* |

**At a glance**

| Metric | Value |
|---|---|
| Sections | 12 + quick reference |
| Interfaces | CLI · MCP · Dashboard (proof) |
| v1 recipe | Flask → FastAPI |
| Eval corpus | 6 pinned repos · 4 tiers · K=3 |
| Stable-tier fault recovery | 100% on fixture (3/3 × 2 scenarios) |
| Green definition | Full suite ∧ all tasks done ∧ zero skips |

**Stack chips:** FastAPI · LangGraph · Postgres 16 · pgvector · LiteLLM · Docker / gVisor · Next.js · FastMCP · SQLAlchemy async · Alembic · pytest

---

## On this page

1. [Architecture Overview](#01--architecture-overview)
2. [Job Lifecycle & Graph Nodes](#02--job-lifecycle--graph-nodes)
3. [Durability Model](#03--durability-model)
4. [Sandbox & Verification](#04--sandbox--verification)
5. [Recovery Strategies](#05--recovery-strategies)
6. [Recipe System (Flask → FastAPI)](#06--recipe-system-flask--fastapi)
7. [Eval Methodology](#07--eval-methodology)
8. [Failure Taxonomy](#08--failure-taxonomy)
9. [Corpus & Admission](#09--corpus--admission)
10. [CLI & MCP Contracts](#10--cli--mcp-contracts)
11. [Auth & Demo Protection](#11--auth--demo-protection)
12. [Stack & Data Model](#12--stack--data-model)
13. [Quick Reference](#qr--quick-reference)

---

## 01 · Architecture Overview

Portage is **one core engine** exposed through **two interfaces**. The autonomous agent + eval harness is the credibility engine; the MCP tools are the product wedge. Sequencing rule: build the moat first, the wedge second.

```
                 Host / EC2 (compose stack)
   ┌────────────────────────────────────────────────────────────┐
   │  [optional] Caddy :80/:443  — TLS + reverse proxy          │
   │       /        → frontend (Next.js) :3000                  │
   │       /api/*   → api (FastAPI)      :8000                  │
   │                                                            │
   │  api ──POST /jobs──► Postgres (jobs queue)                 │
   │  worker ──claim──► LangGraph graph (checkpointed)          │
   │       │                                                    │
   │       ├── Ingest: clone + code graph                       │
   │       ├── Plan / Execute: LLM via LiteLLM                  │
   │       ├── Verify: spawn sandbox (--network none [, gVisor])│
   │       └── Recover / Integrate / Report                     │
   │                                                            │
   │  db: Postgres 16 + pgvector                                │
   │       · asyncpg  → domain tables (Alembic)                 │
   │       · psycopg3 → LangGraph checkpoint tables             │
   └────────────────────────────────────────────────────────────┘

  Separate process (no compose required):
    MCP stdio server → same sandbox + graph primitives
```

### Strategic shape

| Interface | Role | Who triggers |
|---|---|---|
| CLI (`portage`) | Autonomous migrations | Developer / CI |
| MCP server | Verified primitives for other agents | Claude Code, Cursor |
| Dashboard | Observability + eval proof | Humans reading results |

The frontend never owns schema. The backend is the single source of truth. The CLI and dashboard are both thin REST clients — neither touches the queue or DB directly.

### Monorepo layout (operational)

```
apps/backend/src/portage_agent/
  config.py          # Settings; derives both DSNs from POSTGRES_*
  core/              # Protocols: storage, queue, sandbox, llm, retrieval
  db/                # SQLAlchemy models: Job, Task, User, Run, Metric…
  agent/             # LangGraph graph + nodes + runner
  worker/            # FOR UPDATE SKIP LOCKED queue + lease heartbeat
  api/               # FastAPI routes
  recipes/           # flask_to_fastapi (pluggable)
  sandbox/           # Docker adapter + JUnit parsing
  retrieval/         # code-review-graph adapter
  llm/               # LiteLLM ladder
  eval/              # Phase 4 harness → runs/metrics
  cli/               # `portage` console script
  mcp/               # FastMCP stdio tools
  auth/              # GitHub OAuth, refresh families, API keys
corpus/              # corpus.toml + FINDINGS.md + curation log
apps/frontend/       # Next.js dashboard (REST only)
docs/                # METHODOLOGY.md, USAGE.md, assets/
scripts/             # dod_check, phase{1..7}_check, demo_kill_resume
```

### Design constraints that matter

- **Async everywhere** in the backend (handlers, sessions, graph nodes).
- **Interfaces before adapters** — Docker, LiteLLM, storage sit behind `core/` Protocols; provider choice is env, not code.
- **Two DB drivers, one Postgres** — never merge the DSNs; LangGraph owns its tables via `AsyncPostgresSaver.setup()`, Alembic owns domain tables only.
- **Job/Task status is VARCHAR + app-side StrEnum** — keeps Alembic simple as states are added.

---

## 02 · Job Lifecycle & Graph Nodes

### Submission

```http
POST /jobs
Content-Type: application/json

{
  "repo_url": "https://github.com/…/repo",
  "migration_recipe": "flask_to_fastapi",
  "config": {
    "ref": "<commit sha>",
    "subdir": "examples/tutorial",
    "inject_fault": "bad_patch"
  }
}
```

Job lands as `queued`. A worker claims it atomically. Graph state is keyed by `thread_id = job_id`.

### Runner semantics (resume vs restart)

`agent/runner.py` calls `aget_state(config)` first:

| Checkpoint state | Action |
|---|---|
| No checkpoint | `ainvoke(initial_input, config)` — fresh start |
| Pending `next` nodes | `ainvoke(None, config)` — **do not** re-pass input |
| Terminal | No-op |

This is the difference between “resume” and “accidentally restart from Ingest.”

### Node-by-node

#### Ingest
- Clone remote (or bind local/fixture path). Optional `ref` pins a SHA; optional `subdir` lifts a subdirectory out as the workspace root and re-snapshots it as a fresh git repo so rollback/worktree machinery still works.
- Build structural knowledge graph via retrieval adapter (code-review-graph over MCP stdio).
- Persist worktree path + graph summary into graph state / job columns.
- **Idempotent on resume:** if the worktree already exists from a prior claim, Ingest does not re-clone or re-build from scratch.

#### Plan
- Recipe `detect` scans Python files for framework markers (Flask imports, routes, blueprints, templates, sessions, etc.).
- Each file becomes a top-level Task with typed Subtasks (`route_to_endpoint`, `app_factory`, `test_harness`, …).
- **Export-contract AST pass:** for each file, compute names that sibling modules import; state that contract in the Execute prompt. Before this, ~50% flake on a 3-file app from dropped `router` exports.
- Framework-agnostic modules (no Flask import, not a test harness) are left alone — they are the stable core routes call.
- Unknown / mismatched recipe → empty task list → Execute/Integrate no-op → ingest→verify→report degradation (honest red: nothing migrated).

#### Execute
- Walk file tasks in `order_index` order.
- For each task: build prompt from recipe guidance + export contract + prior failing context (if any) → LiteLLM completion → write file → record `content_hash` (sha256) + diff + `attempts_log` entry.
- **Idempotency:** if on-disk content already matches `content_hash`, skip the LLM call (crash mid-Execute safe).
- **Tier selection:** attempts `1..escalate_after_attempts` use driver model; later attempts use escalation model. Both recorded in `attempts_log` with public display labels (private deployment names never leave env).
- Optional `execute_task_delay_seconds` exists solely to create a deterministic kill window for DoD demos.

#### Verify
- Compute blast radius of changed files → scope test args when possible; final honesty bar still requires the **full** suite.
- Spawn ephemeral sandbox container: mount worktree, `--network none`, run pytest, parse JUnit.
- Feed Recover **stdout + stderr** (conftest-chain import errors often appear only on stderr).
- Pass predicates:
  - structured report present
  - failed == 0, errors == 0
  - **passed > 0** (all-skipped suite is a failure — models decorating every test with `@pytest.mark.skip` must not short-circuit recovery)

#### Recover
See [§05](#05--recovery-strategies). Routes back to Plan (replan), Execute (regenerate), or Integrate (give up).

#### Integrate
- Always recompute `git diff` from the worktree against the pre-migration baseline.
- Never trust a cached/stale diff from an earlier attempt (false-green class: empty diff + “green” suite on rolled-back sources).

#### Report
- Reload tasks from Postgres (source of truth), not from in-memory graph state alone.
- Emit `report.json`: task tree, recovery actions, `llm_usage` (calls, tokens, USD), test summary, graph summary, verdict helpers.
- Job status → `done` or `failed`; CLI/dashboard derive the honest green verdict from tasks + tests.

### Routing diagram (logical)

```
                ┌─────────┐
                │ Ingest  │
                └────┬────┘
                     ▼
                ┌─────────┐
           ┌───►│  Plan   │◄── replan (missing file residue)
           │    └────┬────┘
           │         ▼
           │    ┌─────────┐
           │    │ Execute │◄── regenerate (rollback done)
           │    └────┬────┘
           │         ▼
           │    ┌─────────┐     pass
           │    │ Verify  │──────────────► Integrate → Report
           │    └────┬────┘
           │         │ fail
           │         ▼
           │    ┌─────────┐
           └────│ Recover │── budgets exhausted ──► Integrate → Report
                └─────────┘
```

---

## 03 · Durability Model

Durability is the core product edge — not “the LLM is smart,” but “the run survives process death and still tells the truth.”

### Checkpointing
- LangGraph `AsyncPostgresSaver` persists state after every node.
- Key: `thread_id = job_id`.
- Worker dies mid-graph → another worker resumes from the last checkpoint, not from zero.
- Ingest is written so resume does not re-clone / re-graph unnecessarily.

### Queue + lease
- Claim is a single atomic SQL pattern: `UPDATE … WHERE id = (SELECT … FOR UPDATE SKIP LOCKED LIMIT 1)`.
- A job is claimable if:
  - `status = queued`, or
  - `status = running` **and** `heartbeat_at` older than `JOB_LEASE_SECONDS` (worker crashed).
- Heartbeat runs on its own asyncio task with its own DB connection (so a stuck graph node cannot starve the lease).

### Content-hash idempotency
- Each Execute step is keyed by job + task + content hash of the written file.
- Resume after mid-Execute crash skips tasks already applied instead of re-calling the model.

### Kill-and-resume demo

![Kill-resume GIF](../kill-resume.gif)

| Script | Assertion |
|---|---|
| `scripts/demo_kill_resume.sh` | Visual / asciinema demo (`docs/assets/kill-resume.gif`) |
| `scripts/dod_check.sh` | Stricter Phase-0 DoD: kill worker mid-graph → restart → resume → finish |

The eval harness cannot SIGKILL the worker it depends on, so crash-resume is covered by these scripts separately from the K-run grid.

---

## 04 · Sandbox & Verification

### Why a sandbox
Verification must be:
- **Isolated** — untrusted migrated code should not touch the host network or sibling jobs.
- **Reproducible** — same image, same pins, same offline constraint as corpus admission.
- **Structured** — JUnit (or equivalent) parsed into `{total, passed, failed, errors, skipped}` plus failing test names.

### Runtime contract
- Ephemeral Docker container per test run.
- `--network none` — no pip install at test time; every dependency must already be in the image (or vendored).
- Hosted deployments can set `SANDBOX_RUNTIME=runsc` (gVisor) for stronger isolation; local default is the daemon’s runc.
- Image build: `docker compose --profile tools build sandbox`.

### Blast-radius scoping
During iteration, Verify can scope to tests implicated by changed files (via `blast_radius`). The **honesty bar** for green still requires the full suite — scoped runs are a speed lever, not a scoring lever.

### Anti-gaming predicates (learned the hard way)

| Failure mode | What happened | Fix |
|---|---|---|
| False green after skip-and-continue | Recovery rolled worktree to originals; suite passed; report showed stale task counts / empty diff | Report reloads tasks from Postgres; Integrate always recomputes diff; green requires all tasks done ∧ none skipped |
| Skip-out false pass | Model decorated every test with `@pytest.mark.skip`; pytest: total>0, failed=0; Verify treated as PASS | Require **passed > 0**; all-skipped is a failure that must enter Recover |
| Stderr-only crashes | Conftest import errors only on stderr; Recover saw empty errors | Verify feeds Recover stdout **+** stderr |

### MCP reuse
`verify_patch_in_sandbox` is the same sandbox contract, exposed for co-pilot use: copy → apply diff → run → return structured result. The caller’s tree is never modified. That reuse is intentional — the eval proves the loop; MCP sells the loop.

---

## 05 · Recovery Strategies

Recover **classifies** and **rolls back**; Execute owns regeneration; Plan owns replanning.

### Classification inputs
- Last verify output (stdout+stderr).
- Planned file set vs worktree.
- Per-task attempt counts and prior blame targets.
- Budgets: `max_task_attempts` (default 3), `max_recover_visits` (default 4), `escalate_after_attempts` (default 2).

### Crash vs behavioral
Crash markers in pytest output (non-exhaustive): `SyntaxError`, `IndentationError`, `ImportError`, `ModuleNotFoundError`, `cannot import name`, `ERROR collecting`, `errors during collection`.

### Strategy table

| Strategy | Trigger | Action |
|---|---|---|
| **Replan** | Unplanned source file still imports Flask (planner miss) | Route to Plan; append missing task(s). Fault scenario: `drop_task`. |
| **Targeted rollback + regenerate** | Crash traceback implicates specific *planned* files | `git checkout -- <path>` only those files; reset tasks to pending; Execute regenerates with failing output as context. Fault: `bad_patch`. |
| **Widen-on-repeat** | Same lone file implicated twice running | Single-file blame isn’t converging (crash *site* ≠ *offender*); widen to reset all active tasks. Observed rescuing flaskr mid-run when a migrated test calling `create_app()` at import burned the factory’s budget. |
| **Behavioral retry-all** | Assertions fail, no crash | Roll back and regenerate every non-skipped file task; attach failing output. |
| **Model escalation** | Task attempts exceed `escalate_after_attempts` | Execute switches to escalation-tier model; every attempt records `tier` + `model` in `attempts_log`. Fault: `bad_patch_until_escalation`. |
| **Skip-and-continue** | Task hits `max_task_attempts` | Roll file back to original source; mark `skipped`; keep run alive. |
| **Give up → Integrate** | `max_recover_visits` exhausted or nothing left to retry | Integrate + Report with honest red. |

### Self-review retries
Rolled-back attempts keep their failing diff. Retries see it (“debug your own code”) instead of regenerating blind. Effect measured on flaskr: factory went from exhausted-and-skipped (3 blind attempts) to completing all 6 tasks.

### Integrity rule
Skip-and-continue can make the *suite* green by restoring originals. That must never score as a successful migration. Green = suite green **and** every planned task done **and** none skipped.

### Fault-injection scenarios (standing eval cases)

| Fault | Simulates | Expected rescue |
|---|---|---|
| `bad_patch` | Corrupted first migration attempt | Crash classify → targeted rollback → regenerate |
| `bad_patch_until_escalation` | Driver tier keeps failing | Escalation tier takes over (logged per attempt) |
| `drop_task` | Planner miss | Residue detection → replan appends missing task |

Recovery quality is read as a **delta against the same repo’s baseline** green rate. Injected faults stack on organic flake — a single averaged “recovery rate” flatters easy repos and slanders hard ones.

---

## 06 · Recipe System (Flask → FastAPI)

### Why this migration
Flask → FastAPI spans exactly the things deterministic tools cannot do reliably:
- Routing decorators and HTTP methods
- Path / query / body parsing
- Blueprints → APIRouters
- Error handlers → exception handlers
- App factory + config-as-dict semantics
- Test-client seam (`test_client` / `get_json` → `TestClient` / `.json()`)
- Templates, sessions, flash, auth (partial — the frontier)

### Pluggability contract
A recipe declares:
1. **Detection** — which files are in scope
2. **Task types / subtasks** — what transformations each file needs
3. **Per-task `verify_spec`** — how to know a task’s local intent
4. **Prompt guidance / rules** — encoded after observed failures

The graph is recipe-dispatched. A recipe Plan doesn’t know → empty task list → safe no-op degradation.

### Subtask catalogue (v1, representative)

| Subtask | Intent |
|---|---|
| `app_factory` | `Flask()` / `create_app` → `FastAPI()`; `app.config` as plain dict on `app.state.config`; keep factory name/shape |
| `blueprint_to_router` | `Blueprint` → `APIRouter`; preserve export name importers expect |
| `route_to_endpoint` | `@bp.route` → `@router.<method>`; path converters → typed params; preserve status codes |
| `request_parsing` | `request.args` / `get_json` → typed query / body params |
| `error_handler` | `@errorhandler` → `@exception_handler` + `JSONResponse` with same status/body |
| `test_harness` | Rewrite plumbing only; never delete/weaken assertions |
| `templates_render` | Jinja2Templates wiring |
| `sessions_flash` | SessionMiddleware + flash equivalents (no invented packages) |
| `auth_login` | Session-auth guidance for flask-login shaped apps |

### Rules encoded from live failures (examples)

| Rule theme | Failure observed | Encoding |
|---|---|---|
| Status codes | `JSONResponse` wrapping silently overriding `status_code` (201→200) | Explicit status_code / Response guidance |
| Error bodies | `HTTPException` bypassing app handlers (`{"detail"}` vs `{"error"}`) | Prefer app-level handlers matching prior JSON shape |
| Redirects | `redirect()` → must be `RedirectResponse(..., 302)`; 307 default re-sends POST | Explicit 302 |
| Deprecated APIs | `@app.on_event` warning-as-error under pytest | Lifespan handlers (rule 11) |
| Hallucinated packages | Invented `fastapi_flash`, `fastapi_login` | Never invent packages; inline equivalents (rule 12) |
| Env gaps | FastAPI `Form()` needs `python-multipart` | Pin in sandbox image (not a model error) |
| Export contracts | Dropped `router` export broke siblings | AST export-contract pass in Plan |

### Harness seam (oracle honesty)
Tests reach the app through a thin `client` fixture and `body()` helper. Migration may rewrite plumbing; behavioural assertions must keep exact meaning. A migration that only passes because tests got easier is a failed migration.

### What recipes do *not* solve alone
- Cross-file **call-shape** drift (signatures/usage), only names today
- Deep framework-inspecting tests (`flask.session` / `g` / `app.testing` internals)
- Full fidelity for every Flask extension without per-extension sub-strategies

---

## 07 · Eval Methodology

Companion sources in-repo: `docs/METHODOLOGY.md`, `corpus/FINDINGS.md`.

### 7.1 The oracle
Every corpus repo ships a behavioural pytest suite that is **green on the unmodified repo** (verified during admission, in the same sandbox the eval uses). After migration, the *same assertions* must pass against the migrated app.

### 7.2 The honesty bar
A run scores **green** only if all three hold:

1. Full test suite passes  
2. Every planned task completed  
3. Zero tasks rolled back/skipped by recovery  

CLI exit code 0 and the dashboard verdict use the same bar.

### 7.3 Statistical shape
- Every (repo × scenario) cell runs **K times** (headline grid K=3).
- Persist per-run rows (`runs`) + mean±variance aggregates (`metrics`).
- Variance is reported, not smoothed — e.g. `minimal-flask-api` baseline 2/3 green with test-pass variance is a *finding* (motivated export-contract work), not noise.

Reproducibility pins:
- Remote repos pinned to commit SHA (`corpus.toml` rejects unpinned remotes)
- Sandbox image pins dependencies
- Model + config recorded per attempt in `attempts_log`

### 7.4 Fault injection
Standing scenarios promoted from Phase-3 DoD: `bad_patch`, `bad_patch_until_escalation`, `drop_task`. Report recovery as **(fault green − baseline green)** deltas per repo.

### 7.5 Cost
Every LLM call’s tokens and USD (LiteLLM pricing) recorded per attempt, summed per job (`llm_usage`), averaged per cell. Retries and escalations included. Example: microblog ~$1.50/run is mostly recovery rounds — cost-scales-with-recovery is part of the result.

### 7.6 What these numbers do NOT show

| Non-claim | Why |
|---|---|
| Generality across migrations | One recipe, one language, six repos. Architecture is recipe-agnostic; evidence is recipe-specific. |
| Stronger-model lift (when driver == escalation) | If both tiers resolve to the same deployment (e.g. GPT-4o), escalation-rescue measures the *retry-ladder machinery*, not a stronger model. Swap `LLM_ESCALATION_MODEL` to measure real lift (env-only). |
| Big-repo behaviour | Corpus repos are small (≲ ~40 files). Thousand-file horizons unproven. |
| Immunity to prompt-tuning bias | Several recipe rules were added after corpus failures; the grid partly measures a recipe tuned to this corpus. Disclosed, not pretended away. |

### 7.7 Reproduce

```bash
docker compose up -d && docker compose --profile tools build sandbox

# headline grid
docker compose run --rm worker python -m portage_agent.eval \
  --corpus /corpus/corpus.toml --k 3 --scenarios baseline \
  --suite repro-$(date +%s)

# fault scenarios on stable tier
docker compose run --rm worker python -m portage_agent.eval \
  --corpus /corpus/corpus.toml --k 3 \
  --scenarios bad_patch,bad_patch_until_escalation \
  --repos flask-items-fixture,minimal-flask-api \
  --suite repro-faults-$(date +%s)
```

Results land in `runs`/`metrics` and render on the dashboard `/eval` page.

---

## 08 · Failure Taxonomy

Status closes Phase 4 (2026-07-08). Ordered easy → hard. Each entry names status and fix direction.

### Headline grid (`k3-baseline`, K=3)

| Repo | Tier | Green | Avg test-pass | Avg recover | Avg cost | Avg wall |
|---|---|---|---|---|---|---|
| flask-items-fixture | baseline | **3/3** | 1.00 | 0.0 | $0.022 | 10s |
| minimal-flask-api | baseline | **2/3** | 0.67 | 0.3 | $0.013 | 10s |
| flask-restx-api | framework | 1/3 | 0.67 | 3.3 | $0.044 | 17s |
| flaskr | structural | 0/3 | 0.67 | 3.7 | $0.250 | 55s |
| watchlist | structural | 0/3 | 0.67 | 4.0 | $0.261 | 61s |
| microblog | heavy | 0/3 | 0.00 | 4.3 | $1.503 | 165s |

(green = full migration + full suite; avg test-pass gives partial credit — 0.67 means most tests pass but the all-or-nothing bar isn’t met.)

### Fault recovery (`k3-faults`, K=3, stable tier)

| Repo | Scenario | Green | Avg recover | Avg cost |
|---|---|---|---|---|
| flask-items-fixture | bad_patch | **3/3** | 1.0 | $0.029 |
| flask-items-fixture | bad_patch_until_escalation | **3/3** | 2.0 | $0.054 |
| minimal-flask-api | bad_patch | 1/3 | 1.3 | $0.020 |
| minimal-flask-api | bad_patch_until_escalation | 0/3 | 2.0 | $0.027 |

100% on the fixture; degrades on the real repo where baseline already flakes 1-in-3 and injected faults consume retry budget the organic flake then needs.

### Nine categories

1. **Routing / parsing / responses / error handlers** — **SOLVED** by recipe rules. Pitfalls: `JSONResponse` status override; `HTTPException` body shape; redirect 302 vs 307.

2. **Cross-file name contracts** — **SOLVED** structurally via export-contract AST pass. Before: ~50% flake on a 3-file app (dropped `router` export).

3. **Deprecated / hallucinated APIs** — **SOLVED** by rules 11/12 after observation (`@app.on_event`; invented `fastapi_flash` / `fastapi_login`). Class remains open-ended; each instance is cheap to encode once seen.

4. **Environment gaps** — **SOLVED** case-by-case in the sandbox image (e.g. `python-multipart` for `Form()` — took watchlist from collection-crash to all 15 tests executing).

5. **App factory & config** — **MOSTLY SOLVED**. `app.config`-as-plain-dict (model invented `State.update()`), instance-path, lifespan-not-on_event. Residual bugs cluster in rarely-exercised branches (test_config vs instance config).

6. **Templates / sessions / flash / auth** — **PARTIAL**. With Jinja2Templates + SessionMiddleware + session-auth guidance, template apps complete DAGs and most tests pass (flaskr/watchlist avg test-pass 0.67) — but all-or-nothing green isn’t met. The v1 frontier.

7. **Cross-file call-shape drift** — **OPEN**, dominant residual. Multi-file regeneration keeps *signatures* coherent nowhere — flaskr’s `get_db()` drifts between “plain function”, “needs request”, “context manager” (19/24 failures in a final probe). Export contract pins names, not call shapes.  
   **Fix direction:** extend contract pass to signatures/usage snippets, or plan a shared “interfaces” step before per-file migration.

8. **Flask-coupled extensions** (`flask_sqlalchemy`, `flask_restx`) — **OPEN / v1 boundary**, partially cracked. With plain-SQLAlchemy + RESTX guidance + self-review retries, **flask-restx-api reached green 1/3 at K=3** (was: never collected). flask_sqlalchemy path (watchlist) completes but fails behaviorally. Needs per-extension sub-strategies.

9. **Framework-inspecting tests** — **OPEN by design**. Tests asserting on `flask.session` / `g` / `app.testing` internals cannot pass unchanged against FastAPI. Harness rule rewrites plumbing while preserving assertion *meaning* for client/body seams, not deep introspection (e.g. flaskr `test_factory.py::test_config`).

### Recovery & integrity findings (summary)
- Deepest-frame blame + widen-on-repeat — measured rescue path.
- Retry-with-self-review — failing diff retained for next attempt.
- False-green integrity — structural predicates, not policy text.
- Skip-out false pass (2026-07-09) — `passed > 0` required.
- Escalation measured even when driver == escalation model (machinery vs lift).

---

## 09 · Corpus & Admission

### Selection criteria (all must hold)
1. Real Flask app (routes/blueprints/error handlers exercised)
2. Real pytest suite, green on unmodified repo in the offline sandbox
3. Sandbox-runnable offline (`--network none`)
4. Small (roughly ≤ 25 Python files / ≤ 2k LOC for v1 — reliability, not context-window heroics)
5. Licensed for reuse (MIT/BSD/Apache)
6. Pinned SHA for remotes

### Shipped corpus (6 repos / 4 tiers)

| Repo | Tier | Role |
|---|---|---|
| flask-items-fixture | baseline | Bundled offline-clean Phase-2 fixture |
| minimal-flask-api | baseline | First real OSS repo migrated green |
| flask-restx-api | framework | Extension / marshalling wall |
| flaskr (Pallets tutorial) | structural | Templates + factory + auth-shaped flows |
| watchlist | structural | flask_sqlalchemy + sessions |
| microblog | heavy | Multi-extension / long recovery |

Original ≥10 target was traded for a documented finding: **a single shared sandbox image cannot serve mutually incompatible dependency pins** (2017-era Flask 0.12 stacks, abandoned `flask_restplus`, `itsdangerous<2.1`, SQLAlchemy 1.x APIs). Four candidates dropped for that shared cause. Unlock if breadth becomes the goal: **per-repo sandbox images**.

### Sandbox accommodations (honest-oracle preserving)
These stand in for the repo’s *own documented dev setup*, never for test logic:
- Repo root on `PYTHONPATH` (≙ `pip install -e .`)
- `test_args` scoping (≙ the repo’s CI test selection; excludes Selenium/load tests)
- `test_env` (documented test-env vars)
- `PORTAGE_TEST_SETUP` schema-provision hook (documented “provision test DB” step)

### Vetting procedure
1. Clone at pinned SHA; run suite in sandbox image offline → green?
2. One baseline harness run; analyze failures into taxonomy (documented failure is a deliverable).
3. Only then add fault scenarios / higher K.

Helper: `scripts/vet_corpus_repo.sh <git-url> [ref] [test-args…]`.

---

## 10 · CLI & MCP Contracts

### CLI commands

| Command | Purpose |
|---|---|
| `portage migrate <repo> [--ref SHA] [--subdir D] [--recipe R] [--watch]` | Submit + optional live attach |
| `portage status <id>` | Task tree, attempts, verdict |
| `portage jobs [--limit N]` | Recent jobs |
| `portage report <id> [--diff]` | Report JSON; optional full migration diff |

`PORTAGE_API` or `--api` selects the control plane. Exit codes: **0** honest green · **1** red · **2** usage/infra.

### MCP tools

| Tool | Input (conceptual) | Output |
|---|---|---|
| `verify_patch_in_sandbox` | `repo_path`, optional `diff`, `test_args`, `timeout_seconds` | `{ok, applied, passed, tests, failing?, output_tail?, error?}` |
| `repo_graph` | `repo_path` (git root) | `{ok, build: full\|incremental, files_parsed, total_nodes, total_edges}` |
| `blast_radius` | `repo_path`, `changed_files[]` | Impacted files / callers / tests |

Errors return readable dicts, never protocol crashes. Empty diff = “is the suite green as-is?” Malformed diff = `{ok: false, error: "diff does not apply", …}`.

### Intended co-pilot workflow
1. `repo_graph(R)` once  
2. `blast_radius(R, files)`  
3. Draft diff **without writing**  
4. `verify_patch_in_sandbox(R, diff, test_args=affected)`  
5. Green → write to disk; red → iterate on `failing` / `output_tail`

### Portfolio assets for this section
- CLI: `../cli/01-migrate-watch.png`, `02-jobs.png`, `03-status.png`, `04-diff.png`
- MCP: `../mcp/01-verify-catches-bug.png`, `02-repo-graph-blast-radius.png`
- Portal: `../portal/01-dashboard.png`, `02-job-detail.png`, `03-eval-leaderboard.png`
- Durability: `../kill-resume.gif`

---

## 11 · Auth & Demo Protection

Phase 7 (v3 rev-C). Designed so local DoD scripts stay unchanged while hosted demos don’t get burned by unbounded LLM spend.

### Modes
| `AUTH_MODE` | Behaviour |
|---|---|
| `disabled` | Local default — synthetic local admin; scripts unchanged |
| `github` | Hosted — GitHub OAuth is the **sole** provider (no passwords/email flows) |

### Sessions & machines
- Browser: 15-min access JWT (frontend memory) + rotating refresh cookie (`httpOnly`, `/auth` path) with **family reuse-detection**.
- Machines: `pk_` API keys (sha256 at rest, revocable).

### Authorization
- Ownership-or-admin on every `/jobs*` route — **404, never 403** (no existence leak).
- Eval endpoints stay public / aggregate-only (isolation rule for the leaderboard).

### Demo-uptime limits (env-tunable)
| Limit | Default idea | Effect |
|---|---|---|
| Per-user concurrency | 1 | 429 when exceeded |
| Per-user daily jobs | 5 | 429 when exceeded |
| Per-job LLM cost ceiling | $2.00 | Remaining tasks skipped → honest red |
| Global daily spend cap | 0 (off) / set in prod | 503 “at capacity” |

Spend ledger is the same `attempts_log` the eval numbers use.

### Secret redaction
Path deny-list + pattern scrub at every seam where repo content leaves the sandbox (prompt context, retry errors, report diff) — `agent/nodes/redaction.py`.

### Tests / DoD
- `tests/test_auth_service.py` — rotation, reuse, keys, JWT  
- `tests/test_redaction.py`  
- `scripts/phase7_check.sh` — unit suites + live github-mode API: 401s, key auth, cross-user isolation, quota 429, public eval  

---

## 12 · Stack & Data Model

### Runtime stack (decided)

| Concern | Choice |
|---|---|
| Language / package | Python 3.12 · import package `portage_agent` (PyPI name later: `portage-agent`) |
| API | FastAPI |
| Agent | LangGraph + Postgres checkpointer |
| ORM | SQLAlchemy 2.0 async + asyncpg |
| Migrations | Alembic (domain only) |
| LLM | LiteLLM ladder — provider is config |
| Frontend | Next.js App Router, TypeScript, pnpm — REST only |
| Compose | Root `docker-compose.yml`; optional `edge` profile for Caddy |

### Model ladder (conceptual)
| Tier | Role |
|---|---|
| Driver | Default Execute model |
| Escalation | Recovery strategy after N failures (measured) |
| Cheap | Routing / classification (optional) |
| Embeddings | Local sentence-transformers (via code-review-graph) |

Documented defaults evolve with provider availability; swapping models is an env change. Caveat when driver == escalation: rescue rates measure retry machinery, not lift.

### Domain tables (Alembic)

| Table | Purpose |
|---|---|
| `jobs` | Queue row: recipe, status, config, lease (`worker_id`, `heartbeat_at`), report paths, summaries, `user_id` |
| `tasks` | Plan DAG: file tasks + subtasks (`parent_id`), `verify_spec`, `content_hash`, `diff`, `attempts_log` |
| `users` | GitHub identity, role |
| `runs` / `metrics` | Eval harness output; leaderboard contract |
| Auth tables | Refresh families, API keys (sha256), etc. |

LangGraph checkpoint tables live in the **same** database, created by the worker via `AsyncPostgresSaver.setup()` — never put in Alembic.

### Two DSNs
```
POSTGRES_* env
   ├─ sqlalchemy_dsn  = postgresql+asyncpg://…   → domain
   └─ psycopg_dsn     = postgresql://…           → checkpoints
```

### `attempts_log` entry shape (conceptual)
```json
{
  "attempt": 2,
  "tier": "escalation",
  "model": "<display label>",
  "action": "regenerate",
  "reason": "…",
  "tokens": { "prompt": 0, "completion": 0 },
  "cost_usd": 0.0,
  "failing_diff": "…",
  "at": "2026-07-08T…"
}
```

This ledger feeds: recovery timelines in the UI, escalation-rescue queries, per-job cost ceilings, global spend caps, and eval cost metrics.

### Phase map (shipped)
| Phase | DoD | Status |
|---|---|---|
| 0 Skeleton | Kill worker → resume | ✅ |
| 1 Ingest + Sandbox | Repo → test report + graph | ✅ |
| 2 Autonomous recipe | Fixture green end-to-end | ✅ |
| 3 Recovery | Injected faults survived | ✅ |
| 4 Eval harness | K-grid + FINDINGS taxonomy | ✅ |
| 5a CLI | `portage` console script | ✅ |
| 5b MCP | verify / graph / blast_radius | ✅ |
| 6 Dashboard-as-proof | `/eval`, kill-resume GIF, methodology | ✅ |
| 7 Auth & demo protection | GitHub OAuth, quotas, redaction | ✅ |
| 8 Hosting | Single-box compose + Caddy + gVisor | in progress / deploy |

---

## QR · Quick Reference

### Honesty bar
```
green ⇔ full_suite_pass ∧ all_tasks_done ∧ skipped_tasks == 0 ∧ passed > 0
```

### Budgets (defaults)
| Knob | Default |
|---|---|
| `escalate_after_attempts` | 2 |
| `max_task_attempts` | 3 |
| `max_recover_visits` | 4 |

### Fault cheatsheet
| Fault | Rescue |
|---|---|
| `bad_patch` | Targeted rollback + regenerate |
| `bad_patch_until_escalation` | Escalation tier |
| `drop_task` | Replan |

### Reliability boundary (one sentence)
JSON APIs migrate green for ~$0.01–0.02; template/extension apps are the honest frontier — dominant residual is **cross-file call-shape drift**.

### Asset index (copy with this folder)
```
docs/assets/
  kill-resume.gif
  cli/01-migrate-watch.png
  cli/02-jobs.png
  cli/03-status.png
  cli/04-diff.png
  mcp/01-verify-catches-bug.png
  mcp/02-repo-graph-blast-radius.png
  portal/01-dashboard.png
  portal/02-job-detail.png
  portal/03-eval-leaderboard.png
  portfolio/portage.md            ← project page
  portfolio/portage-deep-dive.md  ← this file
```

### Portfolio wiring tips
- Project page hero: badges from `portage.md` + kill-resume GIF as the single durability proof.
- Deep dive “Technical Deep Dive” CTA should land here (same pattern as Knowledge Hub).
- Replace `LIVE_DEMO_URL` once hosted (DuckDNS/Caddy path in the P8 runbook).
- Keep GitHub link: https://github.com/SohailGidwani/Portage
- Prefer aggregate eval numbers on the public page; job-level detail stays behind auth on the live product.

### In-repo sources of truth
| Doc | Role |
|---|---|
| `CLAUDE.md` | Operational summary + phase plan |
| `docs/METHODOLOGY.md` | How numbers are produced / non-claims |
| `corpus/FINDINGS.md` | Failure taxonomy with evidence |
| `corpus/README.md` | Admission criteria + vetting log |
| `docs/USAGE.md` | Every CLI/MCP scenario |
| `code-migration-agent-planV2.md` | Architecture source of truth |

---

*Portage — the overland carry between two navigable waters.*
