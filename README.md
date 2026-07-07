# Portage

> An autonomous code-migration agent that carries a codebase across the gap between two
> frameworks, executing the migration across many files, verifying itself against the test
> suite, recovering from failures, and proving its reliability with an eval harness.

*(A portage is the overland carry between two navigable waters.)* v1 targets **Flask →
FastAPI**, a migration deterministic tools genuinely can't do (routing decorators,
request/response handling, async, blueprints→routers need *understanding*, not mechanical
rewriting). Migrations are pluggable "recipes", so the architecture generalizes; the
differentiator is the durability/recovery story plus the eval harness.

**Status: Phase 4 (eval harness) in progress — the first real OSS repo has been migrated
autonomously** (`minimal-flask-api`, suite green 2/2, ~$0.01, no recovery needed). The
recipe-agnostic harness drives (corpus × fault-scenarios × K runs) through the real
pipeline and persists mean±variance metrics to the `runs`/`metrics` tables; the corpus is
at 3 pinned entries and growing (curation criteria + vetting log in `corpus/README.md`,
including honestly-documented failures — Flask-RESTX marshalling is the first standing
"known limitation").
A submitted job runs the full **Ingest → Plan → Execute → Verify → (Recover) → Integrate →
Report** graph: it clones the repo, builds a structural knowledge graph, plans a per-file task
DAG, migrates each file with an LLM on a git worktree, runs the affected tests in an ephemeral
network-off Docker sandbox, and, when verification fails, classifies the failure and picks a
bounded recovery strategy before reporting honestly. Everything is checkpointed to Postgres,
so killing the worker mid-run resumes from the last node without re-doing finished work.

## What "recovery" means (Phase 3)

A failed Verify routes to the **Recover** node, which classifies the failure and picks one of
three bounded strategies:

- **Targeted rollback + regenerate**: a crash implicating specific planned files rolls back
  only those files (`git checkout -- <path>`) and re-runs Execute on them, with the failing
  test output as added context.
- **Model escalation (measured)**: a task's first attempts use the driver-tier model; repeated
  failures switch it to the escalation tier. Every attempt lands in the task's `attempts_log`
  with its tier and model, so "how often does escalation rescue a task?" is a queryable fact.
- **Replan**: framework residue in a file the planner missed triggers a replan that appends
  the missing task.

Budgets bound everything (`MAX_TASK_ATTEMPTS`, `MAX_RECOVER_VISITS`); a task that exhausts its
budget is rolled back to original source and marked `skipped`; the run stays alive and the
report stays honest. Execute is idempotent (content-hash keyed), so a crash mid-Execute
resumes without re-calling the model for already-applied files.

## Architecture

```
Next.js dashboard ──REST──> FastAPI API ──enqueue──> Postgres job queue
                                                          │  (FOR UPDATE SKIP LOCKED + lease)
                            LangGraph worker <────claim───┘
                                   │ checkpoints every node (thread_id = job_id)
                                   ▼
      Ingest → Plan → Execute → Verify ──pass──> Integrate → Report
                ▲        ▲         │fail                ▲
                │        │         ▼                    │
                └─replan─┴────── Recover ───give up─────┘
                      (regenerate / replan / give up, bounded)
```

- **api**: FastAPI: `POST /jobs`, `GET /jobs`, `GET /jobs/{id}`, `/jobs/{id}/tasks`,
  `/jobs/{id}/report`, `GET /eval/runs`, `GET /health`.
- **eval harness** (`python -m portage_agent.eval`, Phase 4): runs (corpus repos ×
  scenarios × K) through the real queue/worker — scenarios are the phase-3 fault injections
  promoted into standing eval cases — and writes per-run rows + mean±variance metrics to
  the `runs`/`metrics` tables (the dashboard/leaderboard contract). Corpus manifest:
  `corpus/corpus.toml` (pinned SHAs, per-repo `test_args`/`test_env` accommodations).
- **worker**: claims jobs off the Postgres queue (atomic `FOR UPDATE SKIP LOCKED` + heartbeat
  lease) and runs the LangGraph graph, checkpointing at every node.
- **db**: Postgres 16 + pgvector. Alembic owns domain tables (`jobs`, `tasks`); LangGraph
  owns its checkpoint tables (same DB, different driver; no conflict).
- **frontend**: Next.js (App Router, TS), REST only. The observability surface: jobs list
  plus a job-detail view with the task tree, per-file diffs, the per-attempt tier/model
  timeline, and the recovery summary.
- **sandbox**: ephemeral `--network none` Docker container per test run; JUnit-parsed results.
- **LLM**: LiteLLM model ladder; provider is config, not code. Documented default is Claude
  Sonnet on Bedrock; any LiteLLM model string + creds in `.env` works (Azure OpenAI, Gemini,
  Anthropic…). Optional `LLM_*_MODEL_LABEL` vars control what the UI/reports display, so a
  private deployment name never leaves the env.

## Quickstart

```bash
cp .env.example .env         # add LLM creds for migration runs (see comments inside)
docker compose --profile tools build sandbox
docker compose up            # db -> api (runs migrations) -> worker -> frontend
```

- API: <http://localhost:8000> (`/docs` for the OpenAPI UI)
- Dashboard: <http://localhost:3000>

Submit a migration of the bundled fixture Flask app:

```bash
curl -X POST localhost:8000/jobs -H 'content-type: application/json' \
  -d '{"repo_url":"/fixtures/flask_app","migration_recipe":"flask_to_fastapi"}'
```

A recipe the planner doesn't recognize degrades gracefully: the run becomes
ingest→verify→report (test the repo, build its graph, report; no changes).

## Verifying the DoDs

Each phase has a repeatable definition-of-done check:

```bash
docker compose up -d
bash scripts/dod_check.sh     # Phase 0: kill the worker mid-run -> it resumes from checkpoint
bash scripts/phase1_check.sh  # Phase 1: repo -> structured test report + queryable graph
bash scripts/phase2_check.sh  # Phase 2: fixture Flask app autonomously migrated; full suite green
bash scripts/phase3_check.sh  # Phase 3: injected faults survived (rollback, escalation, replan)
bash scripts/phase4_smoke.sh  # Phase 4: harness -> runs/metrics contract (fixture, K=2)
```

`scripts/vet_corpus_repo.sh <git-url> [ref] [test-args…]` vets a corpus candidate: clones at
the pinned SHA and runs its suite offline in the exact sandbox the eval uses.

`phase3_check.sh` runs three deterministic fault scenarios: a corrupted patch (rescued by
rollback+retry), a patch corrupted until escalation (rescued by the stronger model), and a
deliberately dropped plan task (repaired by replan). It asserts each run still ends with the
full suite green plus the expected recovery evidence in the report.

## Layout

- `apps/backend`: Python 3.12, uv. Package `portage_agent`. (`apps/backend/README.md`)
- `apps/frontend`: Next.js (App Router, TS, pnpm). Observability dashboard: full-width
  jobs+eval view (launch form with pinned-ref support, status filters, windowed table),
  job-detail with the live pipeline route, per-file diffs, attempt timelines, recovery.
- `corpus/`: the pinned eval corpus (`corpus.toml`) + curation criteria, vetting log, and
  first findings (`corpus/README.md`).
- `scripts/`: per-phase DoD checks + `vet_corpus_repo.sh`.
- `infra/terraform/`: IaC (minimal; later phases).
- `code-migration-agent-planV2.md`: the full architecture & build plan (**source of truth**),
  with `portage-v2-forward-plan.md` as the reasoning behind the v2 pivot.
- `CLAUDE.md`: stack, conventions, and the phase plan for contributors/agents.

## Roadmap

Phase 0 skeleton ✅ → Phase 1 ingest + sandbox ✅ → Phase 2 autonomous Flask→FastAPI ✅ →
Phase 3 recovery ✅ → **Phase 4 eval harness (in progress)** — harness + `runs`/`metrics`
tables + fault-scenario eval cases are done; remaining: grow the corpus to ≥10 pinned repos,
run the K≥3 grid, and write the failure-taxonomy ("known limitations") report → Phase 5a CLI
→ Phase 5b MCP server (`verify_patch_in_sandbox`, `repo_graph`, `blast_radius`) → Phase 6
leaderboard + packaging. Known open bug: code-review-graph hangs on some real repos'
content (Ingest degrades to no-graph mode after a timeout — migrations still complete, but
without blast-radius test selection). See `CLAUDE.md` for the definition-of-done per phase.
