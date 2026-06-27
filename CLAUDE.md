# CLAUDE.md — Portage

Guidance for Claude Code (and humans) working in this repo. Keep future sessions aligned
with the decisions already made. **`code-migration-agent-plan.md` is the source of truth**;
this file is the operational summary.

## What this is

**Portage** — an autonomous code-migration agent. It takes a repository + a migration goal,
executes the migration across many files over a long horizon, runs the test suite to verify
itself, recovers from failures, and ships with an eval harness that proves its reliability.

v1 evaluates exactly **one** migration: **Pydantic v1 → v2**. The architecture is general
(migrations are pluggable "recipes"); the differentiator is the durability/recovery story
and the eval harness, not breadth.

Governing principle: **narrow + measured beats broad + unproven.**

## Stack (decided — do not re-litigate)

- **Monorepo:** `apps/backend` (Python 3.12, uv) + `apps/frontend` (Next.js App Router, TS,
  pnpm). Compose at root. **No Turborepo/Nx.**
- **Backend:** FastAPI · LangGraph + `langgraph-checkpoint-postgres` · SQLAlchemy 2.0 async +
  asyncpg · Alembic · LiteLLM (Phase 2) · pgvector. **Async throughout.**
- **DB:** Postgres 16 + pgvector (`pgvector/pgvector:pg16`). **Alembic owns domain tables;
  LangGraph owns its checkpoint tables via psycopg — same DB, no conflict.**
- **Frontend:** Next.js, talks to the backend over **REST only, NO DB/ORM.** The backend is
  the single schema source of truth.
- **Python import package:** `portage_agent` (the bare name `portage` is taken on PyPI by
  Gentoo). Published PyPI name later: `portage-agent`.

### Two DB drivers, one Postgres (important)
- SQLAlchemy/asyncpg DSN (`postgresql+asyncpg://`) → domain tables (the `jobs` table).
- psycopg3 DSN (`postgresql://`) → LangGraph's `AsyncPostgresSaver` checkpoint tables.
- Both derive from the same `POSTGRES_*` env in `config.py`. Don't merge them.

## Repo layout

```
apps/backend/src/portage_agent/
  config.py        # Settings; derives both DSNs from POSTGRES_* env
  logging_conf.py  # stdout logging
  core/            # swappable interfaces: storage, queue, sandbox, llm (typed Protocols)
  db/              # SQLAlchemy async base, session, models (Job)
  agent/           # LangGraph graph (start->work->end), checkpointer, run/resume runner
  worker/          # Postgres queue (FOR UPDATE SKIP LOCKED + lease) + worker loop
  api/             # FastAPI app: /health, POST /jobs, GET /jobs/{id}, GET /jobs
  recipes/ retrieval/ sandbox/ llm/ eval/   # stubs — filled in later phases
apps/backend/alembic/   # migrations (Alembic owns domain tables only)
apps/frontend/          # Next.js App Router dashboard (REST client)
scripts/dod_check.sh    # repeatable Phase 0 DoD verification
notes/                  # gitignored personal decision log
```

## Conventions

- **Async everywhere** in the backend (FastAPI handlers, SQLAlchemy sessions, graph nodes).
- **Pin versions.** Backend via `uv.lock` (`uv sync --frozen`); frontend via `pnpm-lock.yaml`.
- **Lint:** `uv run ruff check src` (line length 100; E,F,I,UP,B). Keep it clean.
- **Job `status` is VARCHAR + an app-side `StrEnum`** (`db/models.py`), *not* a native PG
  enum — keeps Alembic migrations simple as states are added.
- **Interfaces before adapters.** New external dependencies (S3, SQS, Docker, LiteLLM) go
  behind a `core/` Protocol with a config-selected adapter — never `import boto3` in core
  logic. This keeps AWS non-load-bearing.
- **Migrations:** Alembic owns domain tables. The API entrypoint runs `alembic upgrade head`
  before serving. LangGraph checkpoint tables are created by the worker via
  `AsyncPostgresSaver.setup()` (idempotent) — never put them in Alembic.
- **Don't over-engineer.** Build one phase at a time (see below). Resist adding the task
  tree / events / metrics tables before the phase that needs them.

## How to run

```bash
cp .env.example .env          # already done; .env is gitignored
docker compose up             # db -> api (migrates) -> worker -> frontend
# API   http://localhost:8000  (/health, /docs, /jobs)
# UI    http://localhost:3000
```

Submit a job: `curl -X POST localhost:8000/jobs -H 'content-type: application/json' \
  -d '{"repo_url":"https://github.com/acme/x","migration_recipe":"pydantic_v1_to_v2"}'`

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

## Phase plan (DoD per phase)

- **Phase 0 — Skeleton ✅ (this session).** Compose up: Postgres+pgvector + FastAPI + a trivial
  LangGraph graph with the Postgres checkpointer that survives a worker restart.
  *DoD:* kill the worker mid-graph, restart, it resumes. Verified by `scripts/dod_check.sh`.
- **Phase 1 — Ingest + Sandbox.** Integrate `code-review-graph` (ingest/repo-map/blast-radius)
  behind the `retrieval` interface; build the ephemeral-Docker sandbox (network-off, capped)
  behind the `sandbox` interface. *DoD:* given a repo → structured test report + queryable graph.
- **Phase 2 — One recipe end-to-end.** Wire LiteLLM (`llm` interface, Bedrock-primary ladder);
  Pydantic v1→v2 Plan→Execute→Verify→green on one small repo. *DoD:* one real repo migrated,
  full suite passes.
- **Phase 3 — Recovery.** Idempotency (job+task+attempt+hash), bounded retries, replan, model
  escalation, rollback (git worktree per task), checkpoint-resume. *DoD:* injected faults survived.
- **Phase 4 — Eval harness.** Commit-pair corpus, metrics (completion / test-pass /
  fault-recovery), fault injection, K-run mean±variance, per-model rows. *DoD:* metrics report
  across ≥10 repos with variance.
- **Phase 5 — Dashboard + demo.** Live task tree, trace timeline, chaos-recovery view,
  leaderboard. *DoD:* the 2-minute demo runs end-to-end.
- **Phase 6 — Packaging.** README + architecture diagram + demo video + methodology writeup.

## Model ladder (Phase 2+, via LiteLLM)

Driver = Claude Sonnet 4.6 (Bedrock). Escalation = Claude Opus 4.8 (a *recovery strategy*).
Cheap tier = Claude Haiku 4.5 or Gemini 2.5 Flash-Lite. Embeddings = local sentence-transformers.
All pluggable so the eval harness can report metrics per model.
