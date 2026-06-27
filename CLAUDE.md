# CLAUDE.md — Portage

Guidance for Claude Code (and humans) working in this repo. Keep future sessions aligned
with the decisions already made. **`code-migration-agent-planV2.md` (v2) is the source of
truth**, with `portage-v2-forward-plan.md` as the reasoning behind the v2 pivot; this file
is the operational summary.

## What this is

**Portage** — an autonomous code-migration agent. It takes a repository + a migration goal,
executes the migration across many files over a long horizon, runs the test suite to verify
itself, recovers from failures, and ships with an eval harness that proves its reliability.

v1 evaluates exactly **one** migration: **Flask → FastAPI** — chosen because deterministic
tools genuinely *can't* do it (routing decorators, request/response handling, async, DI,
blueprints→routers, error handlers need *understanding*, not mechanical rewriting). The
architecture is general (migrations are pluggable "recipes"); the differentiator is the
durability/recovery story + the eval harness, not breadth. (Localized fallback if the
eval-corpus curation proves too heavy: unittest → pytest — recipe + corpus change only.)

Governing principle: **narrow + measured beats broad + unproven.**

### One core, two interfaces (the strategic shape)

Portage is **one core engine** exposed through **two interfaces** — don't collapse it to
either one:

- **Autonomous mode (the hireable headline):** Portage drives the whole migration itself,
  proven by the eval harness. This is the hiring bet — build it first (Phases 2–4).
- **Co-pilot mode — MCP (the product wedge):** Cursor / Claude Code call Portage tools
  (`verify_patch_in_sandbox`, `repo_graph`, `blast_radius`). Built second (Phase 5), it's
  pure upside; the autonomous + eval core is what must land.

The load-bearing insight: the autonomous agent + eval harness is the *credibility engine*
for the MCP product — the eval proves the verify/recover loop works, which is why a dev
would trust `verify_patch_in_sandbox` over a raw sandbox. The hard thing validates the easy
thing. **Sequencing rule: build the moat first, the wedge second.**

## Stack (decided — do not re-litigate)

- **Monorepo:** `apps/backend` (Python 3.12, uv) + `apps/frontend` (Next.js App Router, TS,
  pnpm). Compose at root. **No Turborepo/Nx.**
- **Backend:** FastAPI · LangGraph + `langgraph-checkpoint-postgres` · SQLAlchemy 2.0 async +
  asyncpg · Alembic · **LiteLLM (Phase 2, Bedrock-primary ladder)** · pgvector. **Async throughout.**
- **DB:** Postgres 16 + pgvector (`pgvector/pgvector:pg16`). **Alembic owns domain tables;
  LangGraph owns its checkpoint tables via psycopg — same DB, no conflict.**
- **Frontend:** Next.js — **REST only, NO DB/ORM.** Its job in v1 is the **observability /
  eval / demo surface** (live task tree, trace timeline, chaos-recovery view, leaderboard),
  **NOT the front door.** Devs trigger work via CLI + MCP (Phase 5); the dashboard sits
  *outside* the dev's critical path so it isn't friction. The backend is the single schema
  source of truth.
- **Dev entry points:** **CLI + MCP** (primary, Phase 5). The dashboard is proof, not trigger.
- **Python import package:** `portage_agent` (the bare name `portage` is taken on PyPI by
  Gentoo). Published PyPI name later: `portage-agent`.

### Two DB drivers, one Postgres (important)
- SQLAlchemy/asyncpg DSN (`postgresql+asyncpg://`) → domain tables (`jobs`, `tasks`).
- psycopg3 DSN (`postgresql://`) → LangGraph's `AsyncPostgresSaver` checkpoint tables.
- Both derive from the same `POSTGRES_*` env in `config.py`. Don't merge them.

## Repo layout

```
apps/backend/src/portage_agent/
  config.py        # Settings; derives both DSNs from POSTGRES_* env
  logging_conf.py  # stdout logging
  core/            # swappable interfaces: storage, queue, sandbox, llm, retrieval (Protocols)
  db/              # SQLAlchemy async base, session, models (Job, Task)
  agent/           # LangGraph graph (Ingest→Plan→Execute→Verify→Integrate→Report), runner
  worker/          # Postgres queue (FOR UPDATE SKIP LOCKED + lease) + worker loop
  api/             # FastAPI app: /health, POST /jobs, GET /jobs/{id}, GET /jobs
  retrieval/       # adapter over code-review-graph (graph + blast-radius), via MCP stdio
  sandbox/         # ephemeral network-off Docker execution + JUnit report parsing
  storage/         # LocalStorage artifact backend (s3 later)
  recipes/         # migration recipes; v1: flask_to_fastapi/
  llm/             # LiteLLM provider ladder + model-escalation (Phase 3)
  cli/ mcp/ eval/  # stubs — Phase 4/5
apps/backend/alembic/   # migrations (Alembic owns domain tables only)
apps/frontend/          # Next.js App Router dashboard (REST client; observability surface)
scripts/                # repeatable per-phase DoD checks (dod_check, phase1_check, phase2_check)
notes/                  # gitignored personal decision log
```

## Conventions

- **Async everywhere** in the backend (FastAPI handlers, SQLAlchemy sessions, graph nodes).
- **Pin versions.** Backend via `uv.lock` (`uv sync --frozen`); frontend via `pnpm-lock.yaml`.
- **Lint:** `uv run ruff check src` (line length 100; E,F,I,UP,B). Keep it clean.
- **Job/Task `status` is VARCHAR + an app-side `StrEnum`** (`db/models.py`), *not* a native
  PG enum — keeps Alembic migrations simple as states are added.
- **Interfaces before adapters.** New external dependencies (S3, SQS, Docker, LiteLLM) go
  behind a `core/` Protocol with a config-selected adapter — never `import boto3`/provider
  SDKs in core logic. This keeps AWS/provider choice non-load-bearing.
- **Recipes are pluggable.** A recipe declares detection + task types + per-task
  `verify_spec`. The graph is recipe-dispatched: a recipe the Plan node doesn't know yields
  an empty task list, so Execute/Integrate no-op and the run degrades to ingest→verify→report.
- **Migrations:** Alembic owns domain tables. The API entrypoint runs `alembic upgrade head`
  before serving. LangGraph checkpoint tables are created by the worker via
  `AsyncPostgresSaver.setup()` (idempotent) — never put them in Alembic.
- **Don't over-engineer.** Build one phase at a time (see below). Resist adding the
  events / metrics tables before Phase 4 needs them.

## How to run

```bash
cp .env.example .env          # already done; .env is gitignored
docker compose up             # db -> api (migrates) -> worker -> frontend
# API   http://localhost:8000  (/health, /docs, /jobs)
# UI    http://localhost:3000
```

Submit a job: `curl -X POST localhost:8000/jobs -H 'content-type: application/json' \
  -d '{"repo_url":"/fixtures/flask_app","migration_recipe":"flask_to_fastapi"}'`

Backend dev loop (host): `cd apps/backend && uv sync --extra dev && uv run ruff check src`.

## Durability model (the core edge)

- **Checkpointing:** LangGraph's Postgres checkpointer persists graph state after every node,
  keyed by `thread_id = job_id`. Worker dies → another worker resumes from the last
  checkpoint, not from zero.
- **Queue + lease:** the worker claims a job with a single atomic
  `UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1)`. A job is claimable if
  `queued` OR `running` with a heartbeat older than `JOB_LEASE_SECONDS` (its worker crashed).
  The worker heartbeats on its own asyncio task with its own DB connection.
- **Resume vs restart:** `agent/runner.py` calls `aget_state(config)` first — no checkpoint →
  `ainvoke(initial, config)`; pending `next` → `ainvoke(None, config)` (do **not** re-pass
  input); terminal → no-op.
- **Idempotent Execute:** each Execute step is keyed by job+task+content-hash, so a resume
  after a mid-Execute crash skips tasks already applied instead of re-running them.

## Phase plan (DoD per phase) — revised per plan v2 §15

- **Phase 0 — Skeleton ✅.** Compose up: Postgres+pgvector + FastAPI + a trivial LangGraph
  graph with the Postgres checkpointer that survives a worker restart. *DoD:* kill the worker
  mid-graph, restart, it resumes (`scripts/dod_check.sh`).
- **Phase 1 — Ingest + Sandbox ✅.** Clone → graph (code-review-graph, behind `retrieval`) →
  sandboxed test run (network-off Docker, behind `sandbox`) → structured report, checkpointed;
  Ingest runs exactly once on resume. *DoD:* repo → structured test report + queryable graph
  (`scripts/phase1_check.sh`).
- **Phase 2 — Autonomous recipe end-to-end (Flask→FastAPI).** Plan → Execute → Verify → green
  on one small fixture repo. *DoD:* the fixture Flask app is migrated to FastAPI and its full
  test suite passes, autonomously, checkpointed at every node.
- **Phase 3 — Recovery.** Content-hash idempotency, bounded retries, replan, model escalation
  (Sonnet→Opus as a *measured* recovery strategy), git-worktree rollback, checkpoint-resume.
  *DoD:* injected faults survived.
- **Phase 4 — Eval harness (the hireable core).** Recipe-agnostic harness, curated corpus
  (~10–15 small Flask apps), fault injection, K-run mean±variance, per-model rows.
  *DoD:* metrics report across ≥10 repos with variance. **Don't shortchange this.**
- **Phase 5 — MCP + CLI (the product wedge).** `portage migrate <repo> --recipe
  flask-to-fastapi` CLI; MCP server exposing `verify_patch_in_sandbox` / `repo_graph` /
  `blast_radius`; Claude Code + Cursor configs. *DoD:* Claude Code calls
  `verify_patch_in_sandbox` to test its own work before writing to disk.
- **Phase 6 — Dashboard-as-proof + packaging.** Repurpose the Next.js app into the
  observability/eval/demo surface (live task tree, trace timeline, chaos-recovery view,
  leaderboard). README + architecture diagram + 2-min demo video + methodology writeup.

## Model ladder (Phase 2+, via LiteLLM)

Driver = **Claude Sonnet 4.6 (Bedrock)** — default for Execute. Escalation = **Claude Opus 4.8**
(a *recovery strategy*, Phase 3: default attempts a task, escalates on repeated failure,
measured). Cheap tier = **Claude Haiku 4.5** or **Gemini 2.5 Flash-Lite** (routing/classification).
Embeddings = local sentence-transformers (what code-review-graph uses). All pluggable so the
eval harness can report metrics per model. **The provider is config (LiteLLM model strings +
env), so AWS Bedrock is the documented default but not load-bearing — a key for any provider
swaps it in without code changes.** Caveats (June 2026): Opus 4.7+ dropped temperature/top_p/
top_k (prompt-steer only); the Fable 5 (Mythos) tier is export-suspended — not usable.
