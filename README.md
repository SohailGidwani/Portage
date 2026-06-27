# Portage

> An autonomous code-migration agent that carries a codebase across the gap between two
> framework versions — executing the migration across many files, verifying itself against
> the test suite, recovering from failures, and proving its reliability with an eval harness.

*(A portage is the overland carry between two navigable waters.)* v1 targets **Pydantic v1 → v2**.

**Status: Phase 0 (skeleton).** The durable execution spine is up and verified: Postgres +
pgvector, a FastAPI control plane, and a LangGraph agent worker checkpointed to Postgres that
**survives being killed mid-run and resumes from its last checkpoint**.

## Architecture (Phase 0)

```
Next.js dashboard ──REST──> FastAPI API ──enqueue──> Postgres job queue
                                                          │  (FOR UPDATE SKIP LOCKED + lease)
                            LangGraph worker <────claim───┘
                                   │ checkpoints every node (thread_id = job_id)
                                   ▼
                          Postgres + pgvector
```

- **api** — FastAPI: `GET /health`, `POST /jobs`, `GET /jobs/{id}`, `GET /jobs`.
- **worker** — claims a job off the Postgres queue and runs the LangGraph graph
  (`start → work → end`), checkpointing at every node.
- **db** — Postgres 16 + pgvector. Alembic owns domain tables; LangGraph owns its checkpoint
  tables (same DB, different driver — no conflict).
- **frontend** — Next.js (App Router, TS). REST client only, no DB/ORM.

## Quickstart

```bash
cp .env.example .env
docker compose up            # db -> api (runs migrations) -> worker -> frontend
```

- API: <http://localhost:8000> (`/docs` for the OpenAPI UI)
- Dashboard: <http://localhost:3000>

Submit a job:

```bash
curl -X POST localhost:8000/jobs -H 'content-type: application/json' \
  -d '{"repo_url":"https://github.com/acme/x","migration_recipe":"pydantic_v1_to_v2"}'
```

## Verifying the Phase 0 DoD (crash-recovery)

`scripts/dod_check.sh` submits a job, SIGKILLs the worker while it is mid-`work`, restarts it,
and asserts from the logs that it **resumed from the checkpoint** (the `start` node ran exactly
once; the run marker stamped before the crash is preserved):

```bash
docker compose up -d
bash scripts/dod_check.sh
# -> DoD PASSED: killed mid-work, resumed from checkpoint (start ran once, marker ... preserved).
```

## Layout

- `apps/backend` — Python 3.12, uv. Package `portage_agent`. (`apps/backend/README.md`)
- `apps/frontend` — Next.js (App Router, TS, pnpm).
- `scripts/` — operational scripts (`dod_check.sh`).
- `infra/terraform/` — IaC (minimal; later phases).
- `code-migration-agent-plan.md` — the full architecture & build plan (source of truth).
- `CLAUDE.md` — stack, conventions, and the phase plan for contributors/agents.

## Roadmap

Phase 0 skeleton ✅ → Phase 1 ingest + sandbox → Phase 2 one recipe end-to-end → Phase 3
recovery → Phase 4 eval harness → Phase 5 dashboard + demo → Phase 6 packaging. See `CLAUDE.md`
for the definition-of-done per phase.
