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
| Sections | 13 + quick reference |
| Interfaces | CLI · MCP · Dashboard (proof) |
| v1 recipe | Flask → FastAPI |
| Eval corpus | 7 pinned repos · 4 tiers · K=3–5 |
| Reliability gate (current) | **Flaskr + Watchlist 10/10 at K=5** · full-corpus confirmation **6/7** at K=1 · oracle integrity 1.0 |
| Prior milestone grid | 13/21 strict green (61.9%), i.e. 38.1% red/errored — see §08 for exactly what that was |
| Headline capability | Plans and creates **new** target-architecture modules, not just rewrites |
| Green definition | Full suite ∧ all tasks done ∧ zero skips ∧ oracle integrity 1.0 |

**Stack chips:** FastAPI · LangGraph · Postgres 16 · pgvector · LiteLLM · Docker / gVisor · Next.js · FastMCP · SQLAlchemy async · Alembic · pytest

---

## On this page

1. [Architecture Overview](#01--architecture-overview)
2. [Job Lifecycle & Graph Nodes](#02--job-lifecycle--graph-nodes)
3. [Durability Model](#03--durability-model)
4. [Sandbox & Verification](#04--sandbox--verification)
5. [Recovery Strategies](#05--recovery-strategies)
6. [Recipe System (Flask → FastAPI)](#06--recipe-system-flask--fastapi)
6b. [Artifact-Producing Plans](#06b--artifact-producing-plans)
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
Plan is where every decision is *frozen* — everything downstream consumes checkpointed artifacts rather than re-deciding.

- Recipe `detect` scans Python files for framework markers (Flask imports, routes, blueprints, templates, sessions, extensions, CLI).
- Each file becomes a top-level Task with typed Subtasks (`route_to_endpoint`, `app_factory`, `test_harness`, …).
- **Dependency ordering:** tasks are sorted by a **Tarjan SCC condensation of the real import graph** — providers before consumers, cycle-safe, deterministic (role order breaks ties only). Before this, routers were migrated *before* the modules they import, so importers guessed interfaces their dependencies later contradicted.
- **Interface manifest:** an AST pass records every cross-file symbol siblings bind — with its original signature, lifecycle facts (async/generator/decorator), real call sites, and use-site-derived member shapes (called ⇒ method, read ⇒ attribute). Each symbol gets one frozen *target* decision; recipe pin rules may override, and two rules claiming one symbol fails Plan loudly rather than silently picking one.
- **Architect call + contract compiler:** proposes new artifacts the migration requires, then deterministically completes the fields the engine already derives — see [§06b](#06b--artifact-producing-plans).
- **Oracle manifest:** freezes each test file's protection strategy and its assertion census.
- **Executable cuts:** the sets of files that must reach a mutually coherent framework state before the sandbox can honestly run anything (a migrated `APIRouter` handed to a still-Flask `register_blueprint` is not a testable state). Small cuts become coordinated generation units; large ones stay batch-only.
- Framework-agnostic modules (no Flask import, not a test harness) are left alone — they are the stable core routes call.
- Unknown / mismatched recipe → empty task list → Execute/Integrate no-op → ingest→verify→report degradation (honest red: nothing migrated).
- **Replan may append, never mutate:** frozen contracts bind every retry, escalation, reset, and crash-resume.

#### Execute
- Walk tasks in scheduler order, generating in bounded coordinated units.
- For each task: build the prompt from recipe guidance + the frozen DEFINES/CALLS contract sections + cut topology + prior failing context (if any) → LiteLLM completion → **mechanical AST gates** → write file → record `content_hash` (sha256) + diff + `attempts_log` entry.
- **Pre-sandbox gates** (all reject before a container starts, each with an exact violation message): contract presence and shape (required args may not grow, async/generator-ness may not flip, declared exports must exist); **defined-vs-invented capability ownership**, receiver-aware; provider→consumer import direction (a provider importing its declared consumer is rejected even inside a function); decorator-factory shape (must still return a local wrapper); middleware installation shape (`app.add_middleware(...)`, not bare construction); and **new import cycles** compared against the source import graph.
- A violation earns exactly one accounted repair call that sees its own rejected draft and the exact violations; the better draft wins (parseable beats unparseable, then fewer violations, tie to the repair).
- **Idempotency:** if on-disk content already matches `content_hash`, skip the LLM call (crash mid-Execute safe).
- **Tier selection:** attempts `1..escalate_after_attempts` use driver model; later attempts use escalation model. Both recorded in `attempts_log` with public display labels (private deployment names never leave env).
- Optional `execute_task_delay_seconds` exists solely to create a deterministic kill window for DoD demos.

#### Verify
- Runs the current **executable cut**; the final honesty bar still requires the **full** suite at Integrate.
- Each successfully verified batch is recorded, so a failed repair can return to the last coherent state instead of integrating a mixed one.
- The stale-JUnit trap is closed: the previous report is deleted before every sandbox run (a conftest crash that never rewrote the XML once produced a false green off the *previous* run's results).
- Spawn ephemeral sandbox container: mount worktree, `--network none`, run pytest, parse JUnit.
- Feed Recover **stdout + stderr** (conftest-chain import errors often appear only on stderr).
- Pass predicates:
  - structured report present
  - failed == 0, errors == 0
  - **passed > 0** (all-skipped suite is a failure — models decorating every test with `@pytest.mark.skip` must not short-circuit recovery)

#### Recover
See [§05](#05--recovery-strategies). Routes back to Plan (replan), Execute (regenerate), or Integrate (give up).

#### Integrate
- Full sanctioned suite as the authoritative gate.
- Always recompute `git diff` from the worktree against the pre-migration baseline.
- Never trust a cached/stale diff from an earlier attempt (false-green class: empty diff + “green” suite on rolled-back sources).
- A regression only the full suite catches can route back through Recover **once** (with its own reserved retry), instead of ending the run unrepaired.

#### Report
- Reload tasks from Postgres (source of truth), not from in-memory graph state alone.
- Emit `report.json`: task tree, **artifact plan** (architect status/usage, created paths with their frozen exports/members/consumers, contract-completion audit), **oracle integrity census**, verified batches, executable-cut shapes, unsupported seams, recovery actions, `llm_usage` (calls, tokens, USD — including architect and repair calls), test summaries, `migration_outcome` ∈ `success | failed | unsupported`.
- Job status → `done` or `failed`; CLI/dashboard derive the honest green verdict from tasks + tests + outcome.

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

The run above is real: a migration is submitted, the worker is **SIGKILLed mid-run**, a
replacement worker reclaims the expired lease and resumes from the Postgres checkpoint
(`next=('verify',)`, `loaded_step_log=['ingest','plan','execute']`), and the run finishes
green — with `INGEST` having executed exactly once, so the clone and graph build were
never repeated.

| Script | Assertion |
|---|---|
| `scripts/demo_kill_resume.sh` | The narrated demo behind `docs/assets/kill-resume.gif`; re-record with `vhs scripts/kill-resume.tape` |
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
| **Targeted contract repair** | Failure maps to exactly one frozen contract owner — missing module/export, an import-cycle edge, a known runtime assertion string, or a unique application traceback leaf | Roll back and regenerate **only that artifact** against the failure + its rejected draft + its frozen contract; siblings are content-hash skipped; the whole enclosing cut is re-verified. Runs on its own bounded ledger (`scope=runtime_targeted`) so it never hides ordinary attempt counts. Measured: a stale `.decode()` in one DB module repaired for **$0.011** without regenerating its ten-file cut. |
| **Replan** | Unplanned source file still imports Flask (planner miss) | Route to Plan; append missing task(s). Fault scenario: `drop_task`. |
| **Targeted rollback + regenerate** | Crash traceback implicates specific *planned* files | `git checkout -- <path>` only those files; reset tasks to pending; Execute regenerates with failing output as context. Fault: `bad_patch`. |
| **Widen-on-repeat** | Same lone file implicated twice running | Single-file blame isn’t converging (crash *site* ≠ *offender*); widen to reset all active tasks. Observed rescuing flaskr mid-run when a migrated test calling `create_app()` at import burned the factory’s budget. |
| **Behavioral retry-all** | Assertions fail, no crash | Roll back and regenerate every non-skipped file task; attach failing output. |
| **Model escalation** | Task attempts exceed `escalate_after_attempts` | Execute switches to escalation-tier model; every attempt records `tier` + `model` in `attempts_log`. Fault: `bad_patch_until_escalation`. |
| **Skip-and-continue** | Task hits `max_task_attempts` | Roll file back to original source; mark `skipped`; keep run alive. |
| **Give up → Integrate** | `max_recover_visits` exhausted or nothing left to retry | Integrate + Report with honest red. |

### Self-review retries
Rolled-back attempts keep their failing diff. Retries see it (“debug your own code”) instead of regenerating blind. Effect measured on flaskr: factory went from exhausted-and-skipped (3 blind attempts) to completing all 6 tasks.

### Attribution beats budget (measured)
Two autopsies reconstructed failing runs byte-exact from LangGraph checkpoints and peeled them by hand. Both found the same thing: **whole-file regeneration against an unattributed bug is a paid no-op.** One run spent 19 recover visits and never found a two-line middleware-ordering fix, because every failure surfaced as one identical `ExceptionGroup` whose deepest frames were in framework internals. Another reproduced three identical bugs across two full regeneration rounds. The engineering answer was better *attribution* — contract ownership, import-cycle edges, unique traceback leaves, known runtime assertion strings — not more retries.

### Integrity rule
Skip-and-continue can make the *suite* green by restoring originals. That must never score as a successful migration. Green = suite green **and** every planned task done **and** none skipped **and** `migration_outcome = success` **and** oracle integrity 1.0.

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
Encoded rules are cheap and effective for *known idioms*. They cannot supply a target **architecture** — which is what [§06b](#06b--artifact-producing-plans) exists for — and they don't cover full fidelity for every Flask extension without per-extension surface contracts.

---

## 06b · Artifact-Producing Plans

The capability that moved the hard repos. Everything here is recipe-neutral engine machinery: the core knows only artifacts, contracts, consumers, cuts, verification and rollback; the recipe supplies framework policy.

### Why file rewriting hit a ceiling

Three independent lines of evidence converged:

1. **A manual baseline.** The canonical Flask tutorial app (`flaskr`) was migrated **by hand** under the identical sandbox oracle: **24/24**, with only two lines changed in the test suite (an import swap at a sanctioned plumbing seam). But the winning solution required **four new modules** — a contextvars request-context layer replacing `g`/`session`, a Flask-shaped test surface, a Jinja rendering layer providing the globals the untouched templates consume, and a werkzeug-format password checker. A file-rewriting engine cannot produce that solution at any level of prompt quality. The manual run became the acceptance specification.
2. **A smoking gun in the logs.** On another repo, the model *imported a compatibility module that did not exist*. It had identified the right architecture and had no mechanism to own one, so it hallucinated the import.
3. **A negative result.** Correct executable cuts were implemented first; external green stayed at 0/4. Scheduling boundaries were necessary and demonstrably not the binding constraint.

### The design

| Piece | What it does | Why it's shaped that way |
|---|---|---|
| **Bounded architect call** | One Plan-time call proposes 0–4 `create` artifacts: path, purpose, capabilities, exports, class members, consumers, dependencies, instructions. Strict JSON; deterministic validation; at most two repairs, and a repair is only allowed if it **strictly reduces** the violation count. | A fixed catalogue of module names would permit file creation without architectural agency; letting Execute invent files mid-generation would create unfrozen interfaces and non-transactional hallucinations. |
| **Closed-choice placement** | Paths are selected from a collision-free set derived from the repo's real application roots and a safe module vocabulary; test-shaped names are rejected mechanically. | Path naming was measured as a repeated convergence failure; making it a *selection* rather than free-form removed the class entirely. |
| **Deterministic contract compiler** | After parsing and before validation, the recipe completes what the engine already derives: required consumers, typed module exports (`g`, `session`, …), and uniquely-attributable class members. Wrong kinds are rejected, never overwritten; ambiguous ownership stays a model decision. | Rejecting a structurally correct architecture because the model didn't *echo back* a fact the engine computed is a design flaw. The model keeps judgment (ownership, grouping, class design); the engine supplies bookkeeping. |
| **Frozen contracts** | Created exports enter the same interface manifest, dependency ordering, executable cuts, prompts, diffs, checkpoints, rollback and reporting as rewrites, with provenance `planned_create`. | One decision surface. Retries, escalations, replans, and crash-resumes all converge on the same interfaces. |
| **Ownership-based capability checks** | A framework-shaped capability is valid **only** when a frozen contract owns it, the owner mechanically implements it (constructor-assigned members count), and the consumer is a declared one. Receiver-aware: `app.state.testing` on a raw FastAPI object is rejected. | “Locally defined somewhere” is not ownership. Otherwise a model can launder a hallucination by creating an arbitrary helper with a matching attribute name. |
| **Provider-first topology** | `consumer ∩ depends_on` contradictions and proposal-level cycles are rejected at Plan; providers may not import their declared consumers at import time; module-level providers are ordered before consumer imports. | Generated import cycles were a measured, repeated collection-time failure on multi-package apps. |
| **Action-aware rollback** | `rewrite` restores the worktree HEAD version; `create` removes the file and clears its intent-to-add entry. New files appear in `git diff` from the first write. | The diff stays the authoritative artifact, and rollback stays transactional for both actions. |

Delete/retire actions and arbitrary patch-level repair are deliberately **out of scope** until a measured failure demands them.

### Result

The frozen plan for a `flaskr` migration now contains an application-owned context module exporting real `g`/`session` proxies with the test file as a declared consumer — the same architecture the manual migration used. First fully autonomous green: **12/12 tasks, 24/24 tests, zero recovery visits, $0.154, five model calls**, reproduced in two further independent samples.

### Measurement discipline it forced

Full runs multiply two independent random variables — *architect acceptance* and *generation quality given an accepted plan*. Measuring their product makes every fix unattributable, so the harness gained a **frozen-plan replay mode**: pin an accepted plan, iterate generation/runtime for ~$0.2–0.5 a probe. Replays are diagnostic-only and are excluded from headline leaderboard aggregation — a green under a pinned plan is *not* an autonomous green, and the two are never mixed.

---

## 07 · Eval Methodology

Companion sources in-repo: `docs/METHODOLOGY.md`, `corpus/FINDINGS.md`.

### 7.1 The oracle
Every corpus repo ships a behavioural pytest suite that is **green on the unmodified repo** (verified during admission, in the same sandbox the eval uses). After migration, the *same assertions* must pass against the migrated app.

### 7.2 The honesty bar
A run scores **green** only if all of these hold:

1. Full test suite passes  
2. Every planned task completed, and `migration_outcome = success`  
3. Zero tasks rolled back/skipped by recovery  
4. **Oracle integrity 1.0** — no test deleted, renamed, skipped, or weakened  

CLI exit code 0 and the dashboard verdict use the same bar.

**Oracle integrity, mechanically.** Test files are protected artifacts with a per-file strategy frozen at Plan (`adapter` = byte-preserved, `adapter_wiring`, `sanctioned_normalization`, or explicitly `unsupported`). A census records test names, normalized assertion expressions, `pytest.raises`, parametrization, skip/xfail sites, fixture names and dependencies, decorators, and generator/async lifecycle. Only an explicit normalization list may differ (e.g. `get_json()`→`json()`, `get_data()`→`text`, or an audited import swap to a plan-owned context proxy, recorded line-by-line in the report). Adversarial unit tests prove deletions, renames, added skips, changed `raises`/`parametrize`, and changed fixture lifecycles are all caught at Execute time, before the sandbox.

**Engine errors count against the score.** A run that crashes before producing a report is scored as a failure, and its already-paid planning calls are reconstructed into the cost ledger — a crashed run must not appear cheaper than it was.

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
| Immunity to prompt-tuning bias | Several recipe rules were added after corpus failures; the grid partly measures a recipe tuned to this corpus. Disclosed, not pretended away — and this is exactly what the pending held-out validation (R5) exists to expose. |
| Replay results as autonomous results | Frozen-plan replays isolate generation quality from architect variance. They are diagnostic, tagged as such, and never aggregated into headline green rates. |
| “Recipe-neutral” as a proven claim | The engine contains no corpus identity (verified by literal search over production source), and the ordering/contract/cut machinery is framework-agnostic by construction — but neutrality is only *proven* by a second recipe, which is deliberately deferred. |

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

Ordered easy → hard. Each entry names status and fix direction.

### Historical milestone grid (`eval-full-corpus-k3-20260714`, K=3, fully autonomous)

This is the grid that first activated the artifact-planning capability across the whole corpus. Kept here in full, unedited, because it's the baseline every later number is measured against.

21 autonomous runs, 7 pinned repos, GPT-4o driver+escalation, 252 model calls, **$6.92**, ~18 min aggregate worker time.

| Repo | Tier | Green | Cost | Readout |
|---|---|---|---|---|
| flask-structural-fixture | baseline | **3/3** | $0.18 | stable structural seam coverage |
| minimal-flask-api | baseline | **3/3** | $0.13 | stable external baseline |
| flask-restx-api | framework | **3/3** | $0.21 | the old extension wall, now stable (4/4 tests, ~1 recovery visit each) |
| flaskr | structural | **2 green** / 1 engine error | $0.31 | both completed runs 24/24 with **zero recovery**, ~$0.154 each |
| flask-items-fixture | baseline | 2/3 | $0.12 | one 5/6 test-harness semantic miss |
| watchlist | structural | 0/3 | $0.58 | Flask-SQLAlchemy / config realization |
| microblog | heavy | 0/3 | $5.34 | one stable import-cycle root cause, 26-task migrations |

**Strict autonomous score: 13/21 (61.9%)** — two engine errors are counted as failures, not excused. Prior grid on the same corpus: **6/21 (28.6%)**, externals 0/15; now externals are **8/15**. Oracle integrity was **1.0 on all 19 report-bearing runs**; every red restored and re-verified its original suite.

Reading the reds honestly: three of the eight non-greens are one repo (microblog) failing the same way three times, and it consumed **77% of the grid's cost** — a systematic root cause, not variance. The two engine errors were deterministic-renderer defects that escaped without a report; both have since been made recoverable and reportable.

### What the 38.1% (8/21) actually was

Every red and engine error in that grid was root-caused off its own checkpoint — none were rerun quietly. This is the complete list; each row also states what closed it and when:

| # | class | repo(s) | exact failure | closed by |
|---|---|---|---|---|
| 1 | test-harness semantic drift | items fixture (1 run) | migrated test helper did `resp.json().get("detail", resp.json())` to unwrap FastAPI's error envelope — worked for error responses, crashed on a valid JSON *list* response | harness rule now separates plumbing translation (`get_json()`→`json()`) from response-shape edits; the oracle census rejects a helper that changes assertion-relevant behavior |
| 2 | engine/reporting defect | flaskr (1 run) | deterministic file renderer silently omitted a frozen `testing` export; the job died with **no report at all** | renderer made exception-safe: a rejected result now falls back to ordinary LLM generation instead of aborting, and always produces a report |
| 3 | engine/reporting defect | watchlist (1 run) | same renderer bug, different frozen export (`render_template_with_context`) | same fix as #2 |
| 4 | extension-surface gaps | watchlist (2 runs) | Flask-SQLAlchemy's `db.Model`/session surface only partly realized; an invalid FastAPI-shaped session-middleware import slipped past generation; inherited uppercase config was dropped → `KeyError: 'SECRET_KEY'` | SQLAlchemy extension providers now realize their full frozen surface (`Model`, `metadata`, `event`, `case`, `session`, `first_or_404`, `get_or_404`, `init_app`, `paginate`) for both class and mapping facades; invalid middleware imports normalized to the real Starlette class; `config.from_object` completion merges in every missing uppercase default |
| 5 | import-cycle collection failure | microblog (3 runs, identical) | `app/__init__.py` imports `app.main.routes.before_request`; `app/main/routes.py` imports `db` from the still-initializing `app` package — a cycle the source never had | new import cycles are now rejected twice: once at generation time (provider files may not import their declared consumers) and once at Verify (source vs. migrated import graph compared before any sandbox starts) |

All five classes are closed on the current engine, and every fix is source-derived (AST facts, frozen contracts, import-graph comparison) — none select a repository, file path, or test name.

### Movement, in one line per repo

| Repo | 2026-07-08 | 2026-07-14 | 2026-07-23 |
|---|---|---|---|
| flaskr | 0/3, avg test-pass 0.67 | 2 green at 24/24, zero recovery | **5/5 at K=5**, plus 24/24 in the full-corpus confirmation |
| watchlist | 0/3, suite failed to complete | 0/3, but the suite collects and executes all 15 tests | **5/5 at K=5**, plus 15/15 in the full-corpus confirmation |
| flask-restx-api | 1/3 | 3/3 | 3/3 (K=3 gate holds) |
| minimal-flask-api | 2/3 | 3/3 | 3/3 (K=3 gate holds) |
| microblog | 0/3, 0.00 test-pass | 0/3, migrations reach test execution | accepted-plan replay 26/26 tasks, 4/4 tests, zero recovery — autonomous run still shows architect-proposal variance (below) |

### Fault recovery
Standing scenarios (`bad_patch`, `bad_patch_until_escalation`, `drop_task`) are green on the current engine, verified after each scheduler change. The full fault matrix is deliberately deferred until the baseline improves — running it against a moving baseline would produce numbers that can't be compared to anything. Recovery quality is always reported as **(fault green − baseline green)** per repo; a single averaged “recovery rate” flatters easy repos and slanders hard ones.

### Where it stands now (2026-07-23) — the 38% is gone, replaced by one named residual

The engine that produced the 61.9%/38.1% grid above is not the engine running today. The batch that closed it (**coherent-cut preservation**) attacked the *mechanism*, not the five failure classes individually: before this, a single bad file inside a multi-file verification cut triggered a full rollback of every file in that cut, so one local mistake could sink an otherwise-correct ten-file migration. Recover now checkpoints the last coherent state before attempting a targeted repair and restores *that* — not the original sources — when a repair fails; a shared gate (caller bindings, capability ownership, import direction, cycle rejection, contract shape) runs identically across every generation path — first draft, contract repair, and targeted repair — instead of four separately-maintained checks that could silently drift apart.

**Reliability-gate history, disclosed in full** (this is every generation the gate went through, not just the passing one — shown specifically so a 5/5 doesn't read as rerun-until-green):

| gate generation | Flaskr (K=5) | Watchlist (K=5) |
|---|---:|---:|
| v1 | 2/5 | 5/5 |
| v2 | 3/5 | 3/5 |
| v3 | 3/5 | 5/5 |
| **v4** (current code) | **5/5** | **5/5** |

Items, RESTX, Structural, and Minimal each independently hold their own **3/3** K=3 gate on the same code.

**Full-corpus confirmation** — one fresh autonomous sample per repo, all seven in the same sitting, suite `r4-final-external-k1-20260723`:

| Repo | Result | Tasks | Cost |
|---|---|---:|---:|
| flask-items-fixture | green | 4/4 | $0.0234 |
| flask-structural-fixture | green | 6/6 | $0.0658 |
| minimal-flask-api | green | 6/6 | $0.0348 |
| flaskr | **green**, 24/24 tests | 12/12 | $0.2277 |
| watchlist | **green**, 15/15 tests | 13/13 | $0.2234 |
| flask-restx-api | green | 6/6 | $0.0835 |
| microblog | red | 1/23 | $1.3093 |

**6/7 green.** The sole red is qualitatively different from anything in the 38.1% table above — it is not a capability gap, it's a *planning*-stage variance:

> Microblog's bounded architecture call occasionally proposes a malformed relationship graph (a duplicate provider/consumer entry between two artifacts). Strict validation catches this and rejects the proposal; the run falls back to an ordinary rewrite-only plan; four rewrite files then correctly fail the exact same contract gates that closed failure-class #5 above; the worktree is restored to a coherent state. **Oracle integrity stayed 1.0 and the run was never mislabeled green.**

The underlying migration capability is proven separately from this variance: replaying microblog's own *accepted* architecture (removing the architect call from the loop entirely) reaches **26/26 tasks, 4/4 tests, zero recovery** — repeatably. So the honest framing is: Portage can migrate microblog; its one-shot architecture proposal for microblog doesn't yet converge every time. That is now the single open item standing between the current engine and a clean sweep of the development corpus. It has not yet been formally K-gridded on its own (a $1.31/run repo makes that an expensive dial to turn), and — same discipline as everywhere else in this doc — a fresh 21-sample grid replicating the original design hasn't been re-run at that scale, so the table above is a confirmation, not a final statistic. The next required steps before any launch claim: re-run the fault-injection matrix against this recovery machinery (it's changed enough to warrant it), and run the frozen recipe once against held-out repositories never used during any of this tuning — the number that actually tests whether ~9,400 lines of source-derived rules generalize or just memorized this corpus.

### Ten categories

1. **Routing / parsing / responses / error handlers** — **SOLVED** by recipe rules. Pitfalls: `JSONResponse` status override; `HTTPException` body shape; redirect 302 vs 307.

2. **Cross-file name contracts** — **SOLVED** structurally via export-contract AST pass. Before: ~50% flake on a 3-file app (dropped `router` export).

3. **Deprecated / hallucinated APIs** — **SOLVED** by rules 11/12 after observation (`@app.on_event`; invented `fastapi_flash` / `fastapi_login`). Class remains open-ended; each instance is cheap to encode once seen.

4. **Environment gaps** — **SOLVED** case-by-case in the sandbox image (e.g. `python-multipart` for `Form()` — took watchlist from collection-crash to all 15 tests executing).

5. **App factory & config** — **MOSTLY SOLVED**. `app.config`-as-plain-dict (model invented `State.update()`), instance-path, lifespan-not-on_event. Residual bugs cluster in rarely-exercised branches (test_config vs instance config).

6. **Templates / sessions / flash / auth** — **SOLVED for the canonical case**, and the reason is architectural rather than prompt-level: the app gets an owned request-context artifact (contextvars-backed `g`/`session` proxies), an owned rendering layer supplying the globals that untouched templates consume, and a session middleware installed in the correct order. `flaskr` — templates + factory + auth flows + SQLite + Click CLI — now migrates autonomously at 24/24 with zero recovery. Extension-heavy variants (below) are not yet at this level.

7. **Cross-file call-shape drift** — **SOLVED structurally**; formerly the dominant residual (19/24 failures in one probe, `get_db()` drifting between plain function / needs-request / context manager across files). Three mechanisms closed it: SCC-condensation dependency ordering so providers migrate first; a frozen interface manifest carrying signatures, lifecycle facts and use-site-derived member shapes; and pre-sandbox AST enforcement of both the DEFINES and CALLS sides.

8. **Flask-coupled extensions** — **SOLVED**. `flask_restx` was the first to close (3/3 green at K=3, from 1/3) — the strongest generality evidence in the corpus, since none of the contract machinery was built against it. `flask_sqlalchemy` (watchlist) followed once extension providers realized their complete frozen surface for both class and mapping facades (`Model`, `metadata`, `event`, `case`, `session`, `first_or_404`, `get_or_404`, `init_app`, `paginate`, with real pagination — not a stub): watchlist now reaches autonomous **15/15** and holds a **5/5 K=5 gate**.

9. **Framework-inspecting tests** — **SOLVED via ownership, not exemption**. Tests asserting on `flask.session` / `g` / `app.testing` internals can now pass because the plan *owns real implementations* of those surfaces and the engine performs an audited line-level import swap to them; the assertion census proves nothing else changed. `flaskr`'s `test_factory.py::test_config`, `test_db.py`, and the CLI-runner test — the three hardest framework-inspecting tests in the corpus — all pass in the autonomous green runs.

10. **Provider initialization / import cycles in multi-package apps** — **SOLVED for generation and runtime; one planning-stage residual remains**. New import cycles are now rejected twice — once at generation time (a provider file may not import a file declared as its consumer) and once at Verify, by comparing the source and migrated import graphs before any sandbox starts. Microblog's accepted architecture now replays to **26/26 tasks, 4/4 tests, zero recovery**, repeatably. What's left is upstream of generation entirely: the one-shot architecture proposal for this repo occasionally produces a malformed relationship graph, which strict validation correctly rejects — see "Where it stands now" above.

### Recovery & integrity findings (summary)
- Coherent-cut preservation — a failed targeted repair restores the last-known-good checkpoint, not the original sources; this closed most of the 38.1% grid above and is the single highest-leverage fix in the project's history.
- One shared generation gate — caller/capability/import-direction/cycle/contract checks run identically across first-draft, contract-repair, and targeted-repair paths instead of four checks that could drift apart.
- Targeted contract repair — one owner, one bounded ledger, whole cut re-verified.
- Attribution beats budget — regeneration against unattributed bugs measured as a paid no-op.
- Deepest-frame blame + widen-on-repeat — measured rescue path.
- Retry-with-self-review — failing diff retained and shown to the next attempt.
- False-green integrity — structural predicates, not policy text.
- Skip-out false pass — `passed > 0` required; generalized into the Execute-time oracle census.
- Stale-JUnit false green — the report file is deleted before every sandbox run.
- Escalation measured even when driver == escalation model (machinery vs lift), with a controlled two-model experiment on record to separate them.

---

## 09 · Corpus & Admission

### Selection criteria (all must hold)
1. Real Flask app (routes/blueprints/error handlers exercised)
2. Real pytest suite, green on unmodified repo in the offline sandbox
3. Sandbox-runnable offline (`--network none`)
4. Small (roughly ≤ 25 Python files / ≤ 2k LOC for v1 — reliability, not context-window heroics)
5. Licensed for reuse (MIT/BSD/Apache)
6. Pinned SHA for remotes

### Shipped corpus (7 repos / 4 tiers)

| Repo | Tier | Role |
|---|---|---|
| flask-items-fixture | baseline | Bundled offline-clean Phase-2 fixture |
| flask-structural-fixture | baseline | Bundled generic structural fixture — factory + `g`/`current_app` SQLite + Click + blueprint + pytest wiring (added so structural regressions are catchable for ~$0.04 instead of $0.30) |
| minimal-flask-api | baseline | First real OSS repo migrated green |
| flask-restx-api | framework | Extension / marshalling wall — now 3/3 |
| flaskr (Pallets tutorial) | structural | Templates + factory + auth-shaped flows + CLI; **the acceptance benchmark** (a hand migration under the same oracle defines the target) |
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
| `portage migrate <repo> [--ref SHA] [--subdir D] [--recipe R] [--watch]` | Submit + optional live attach (progress bar, created artifacts, task states) |
| `portage status <id>` | Outcome · full suite · plan completion · oracle integrity · recovery · LLM cost |
| `portage jobs [--limit N]` | Recent runs with honest outcomes |
| `portage report <id>` | Structured run report (JSON) |
| `portage diff <id> [--output F] [--open] [--stat]` | The migration patch — view, save, or open in an editor |

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
| 8a Deploy-ready repo | Caddy edge profile, loopback-only ports, API root-path, gVisor runtime flag | ✅ |
| **R Recipe excellence** | **Depth before breadth (current phase)** | see below |
| 8b Hosting | Single-box EC2 + DuckDNS + Caddy TLS; runbook written | parked until R closes |

### Phase R (current) — recipe excellence

After Phase 6, external review made the call that shapes everything since: the strongest claim is *depth*, not breadth — “reliably performs one genuinely difficult migration, measures where it fails, and recovers better than a generic coding agent.” Deployment was **parked by decision** (the repo is already deploy-ready) until the recipe meets a measured readiness bar.

| Stage | Content | Status |
|---|---|---|
| R1 | Cross-file interface consistency: SCC ordering, frozen interface manifest, DEFINES/CALLS contracts, mechanical contract checks | ✅ implemented |
| R2 | Recovery completeness: Integrate→Recover routing, per-cut verification batches, failure fingerprints | ✅ implemented |
| R3 | Oracle protection: assertion census, deterministic test-compat facade, explicit `success/failed/unsupported` outcomes | ✅ gate closed |
| R4 | Artifact-producing plans + idiom profiles ([§06b](#06b--artifact-producing-plans)) | ✅ shipped, incl. extension surfaces (`flask_restx`, `flask_sqlalchemy`) |
| R5 | **Held-out validation** — 3–5 fresh pinned repos never touched during development; freeze the recipe; publish dev vs held-out side by side | pending — the last gate before launch |

**Readiness bar (exit criteria, set before the work):** JSON-API tier ≥90% green · template/session tier 70–80% · extension tier supported or honestly rejected · no fault-scenario degradation · no false greens or weakened tests · **reproduced on held-out repositories**. The development-corpus side is now largely met — Flaskr and Watchlist each hold a 5/5 K=5 gate, Items/RESTX/Structural/Minimal each hold 3/3, and a fresh full-corpus sweep went 6/7 (see §08) — but **the bar as written requires held-out reproduction, and R5 hasn't run.** A recipe with ~9,400 lines of source-derived rules (spread across `_flask_analysis.py`, `_flask_runtime.py`, `_flask_web.py`, and the `flask_to_fastapi.py` orchestrator) that only proves itself on the repos that shaped those rules hasn't proven itself yet; that's what R5 is for, and until it runs the bar is not met.

---

## QR · Quick Reference

### Honesty bar
```
green ⇔ full_suite_pass ∧ all_tasks_done ∧ skipped_tasks == 0
        ∧ passed > 0 ∧ migration_outcome == "success"
        ∧ oracle_integrity == 1.0
```

### Budgets (defaults)
| Knob | Default |
|---|---|
| `escalate_after_attempts` | 2 |
| `max_task_attempts` | 3 |
| `max_recover_visits` | 4 |
| `max_targeted_contract_repairs` | 1 (separate ledger from ordinary attempts) |
| architect calls | 1 + at most 2 strictly-improving repairs |
| created artifacts per plan | ≤ 4 |

### Fault cheatsheet
| Fault | Rescue |
|---|---|
| `bad_patch` | Targeted rollback + regenerate |
| `bad_patch_until_escalation` | Escalation tier |
| `drop_task` | Replan |

### Reliability boundary (one sentence)
Every repo in the development corpus now migrates green repeatably — JSON APIs, RESTX-style APIs, and both hard structural/extension apps (Flaskr, Watchlist) — except microblog, whose migration *capability* is proven (26/26 tasks on its accepted plan) but whose one-shot architecture proposal doesn't converge every time; **held-out validation (R5), not corpus difficulty, is the honest frontier now.**

### Headline numbers (current, 2026-07-23)
| Metric | Value |
|---|---|
| Reliability gate | **Flaskr 5/5 · Watchlist 5/5 at K=5**; Items/RESTX/Structural/Minimal 3/3 at K=3 |
| Full-corpus confirmation | **6/7 green**, one sample each, `r4-final-external-k1-20260723` |
| flaskr (acceptance benchmark) | 24/24 tests · 12/12 tasks · 0 recovery · $0.15–0.23 across every measured sample |
| watchlist | 15/15 tests · 13/13 tasks · 0–1 recovery · $0.22 |
| microblog | accepted-plan replay 26/26 tasks, 4/4 tests, 0 recovery; autonomous proposal variance is the one open item |
| Historical milestone grid | 13/21 strict green (61.9%) on 2026-07-14 — see §08 for the full breakdown of that 38.1% |
| Oracle integrity | 1.0 on every report-bearing run, then and now |
| Backend test suite | 292 passing |
| Still pending before launch | fault-injection matrix re-run · R5 held-out validation · `runs`-table reconciliation for harness-death cases |

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
