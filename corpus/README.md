# Eval corpus

The pinned benchmark set the Phase-4 harness runs against (`corpus.toml`). The corpus is
**the long pole of Phase 4** — target: 10–15 small Flask apps with real test suites.

## Selection criteria

A candidate repo qualifies when ALL of these hold:

1. **Real Flask app** — routes/blueprints/error handlers actually exercised, not a
   hello-world. App-factory pattern is a plus (it's a task type the recipe handles).
2. **Real pytest suite** — behavioural tests hitting endpoints via the test client, green
   on the unmodified repo (the honest-oracle precondition: if it isn't green before, "green
   after" proves nothing).
3. **Sandbox-runnable offline** — the test run happens under `--network none`, so every
   test dependency must be in the sandbox image (see `apps/backend/sandbox/
   Dockerfile.sandbox`) or vendored. Prefer repos with few third-party deps; extending the
   image with a widely-used dep (e.g. flask-sqlalchemy + sqlite) is allowed — pin it.
4. **Small** — roughly ≤ 25 Python files / ≤ 2k LOC. The eval measures migration
   reliability, not context-window heroics (that's a later, separate finding).
5. **Licensed for reuse** — MIT/BSD/Apache. Record the license in the entry notes.
6. **Pinned** — remote repos get `ref = "<commit sha>"`. Never track a moving branch.

## Vetting procedure (per candidate)

1. Clone at the pinned SHA; run its suite in the sandbox image (`docker run --rm
   -v $(pwd):/repo -w /repo --network none portage-sandbox:latest run-tests`).
   Green → criterion 2+3 hold. Record test count.
2. Submit one baseline harness run (`--repos <name> --k 1`). Inspect the report: full
   suite green post-migration? If not, the failure gets analyzed for the
   **known-limitations finding** (a documented failure taxonomy is a deliverable, not an
   embarrassment — plan v2 explicitly wants failures analyzed honestly).
3. Only then add fault scenarios / higher K.

## Entry format

```toml
[[repos]]
name = "some-flask-app"          # unique, kebab-case
repo_url = "https://github.com/owner/repo"
recipe = "flask_to_fastapi"
ref = "abc1234…"                 # pinned commit SHA (remote repos)
source = "github"
notes = "License MIT. 12 tests. Uses flask-sqlalchemy (baked into sandbox image @ x.y.z)."
```

## Status

Candidate source: `portage-corpus-candidates.md` (repo root). Vetting log (2026-07-07):

**In corpus (green in sandbox at pinned SHA):**
- [x] `flask-items-fixture` — bundled, offline-clean (the Phase-2 fixture).
- [x] `minimal-flask-api` @ `91ae6ab` — baseline tier, 2 tests (locust excluded via test_args).
- [x] `flask-restx-api` @ `6e64a22` — framework tier, 4 tests (flask-restx baked).

**Parked (needs more accommodation):**
- `flask_for_startups` @ `3cc7ba4` — deps solved (sqlalchemy/flask-login/marshmallow/bcrypt/
  bleach/email-validator baked) and the `PORTAGE_TEST_SETUP` schema hook gets 2/8 green, but
  the remaining tests assume PG-backed transaction semantics the sqlite substitute can't
  honor. Unlock = a DB-sidecar sandbox variant (breaks pure --network none; design later).

**Dropped (with reasons — keep for the methodology writeup):**
- `flask-pytest-example` — package-by-checkout-dir-name convention (root `__init__.py`,
  imports `flask_pytest_example.*`); incompatible with uuid-named workspaces. 2 tests only.
- `todo-list-python` — pymongo + requires a running MongoDB: impossible offline.
- `sdetAutomation/flask-api` — a connexion (OpenAPI-first) app, not plain Flask; out of the
  v1 recipe's scope (candidate for a future `connexion_to_fastapi` recipe).

**Still to vet** (from the candidates doc): flask-celery, flasky, microblog, testing-goat,
pallets/flask examples/tutorial (needs `subdir` support), watchlist, todoism,
flask-sqlalchemy-tutorial.

## Template-app session (2026-07-08, suites template-recipe-1..4, honest-1)

Recipe grew template/session/auth support (subtasks: `templates_render`, `sessions_flash`,
`auth_login`, `request_context`; factory `app.config`-as-dict pattern; structure-preserving
harness rule; classifier fixes: factory-after-routers, test-files-as-harness). Recovery
grew **widen-on-repeat** (same lone crash target twice → reset all active tasks — escapes
deepest-frame blame mis-assignment; demonstrated rescuing flaskr mid-run). Sandbox gained
`python-multipart` (FastAPI `Form()` hard-requires it — environment gap, not model error).

**Integrity fixes (important):** a fully-rolled-back run used to score GREEN (original
app's suite passes!) with a stale 5/6 task count and stale diff. Now: Report reloads tasks
from Postgres, Integrate always recomputes the diff, and harness green ⇐ suite green AND
all tasks done AND none skipped. `template-recipe-4`'s flaskr "GREEN 24/24" was such a
false green — reclassified honestly (suite `honest-1`: red, 5/6, factory skipped).

Current honest state of the template tier: flaskr reaches 5/6 (factory keeps failing —
instance-path/config edge branch), watchlist runs all 15 tests but fails behaviorally on
the flask_sqlalchemy wall (taxonomy #7's hardest: `db` object + `app.extensions` woven
through app AND tests). microblog untested since. **JSON APIs remain reliably green.**

## Grid results (suite `corpus-grid-1`, baseline K=1, all 6 repos, 2026-07-08)

| repo | tier | result | recovery | cost | wall |
|---|---|---|---|---|---|
| flask-items-fixture | baseline | **GREEN 6/6** | 0 | $0.022 | 11s |
| minimal-flask-api | baseline | **GREEN 2/2** | 0 | $0.010 | 9s |
| flaskr | structural | red | 5 visits | $0.062 | 32s |
| watchlist | structural | red | 5 visits | $0.049 | 24s |
| microblog | heavy | red | 5 visits | $0.319 | 61s |
| flask-restx-api | framework | red | 3 visits | $0.019 | 13s |

The headline pattern: **JSON APIs migrate green; template-rendering apps are the current
frontier.** New taxonomy entries from the reds:

5. *Deprecated-API usage* (flaskr): model used `@app.on_event(...)`; pytest promotes the
   DeprecationWarning to an import error. → recipe rule 11 (use lifespan handlers).
6. *Hallucinated packages* (watchlist): model invented `fastapi_flash` to stand in for
   Flask's `flash()`. → recipe rule 12 (never invent packages; inline session-based
   equivalent). Rules 11/12 added 2026-07-08, **not yet re-verified by a grid run**.
7. *Template/session idioms* (all three): `render_template`/`flash`/`url_for`/`session`
   is a genuinely harder migration class than JSON APIs — the tier-6/8 wall the candidates
   doc predicted. Next lever: recipe support for Jinja2Templates wiring + SessionMiddleware.

## First results (suite `corpus-run-2`, baseline K=1, 2026-07-07)

| repo | result | notes |
|---|---|---|
| minimal-flask-api | **GREEN 2/2**, 0 recovery, $0.0096 | first real OSS repo migrated autonomously |
| flask-restx-api | red 0/0, 3 recover visits + escalation, $0.018 | tier-7 failure, analyzed below |

**Known-limitations findings so far** (feeds the Phase-4 taxonomy writeup):
1. *Blueprint-level error handlers* (taxonomy #4): model produced `router.exception_handler`,
   which doesn't exist — APIRouter has no exception handlers. Fixed with recipe rules 9/10
   (generic FastAPI facts); minimal-flask-api went green after.
2. *Cross-file naming contracts* (taxonomy #5/#7): a migrated importer expected `router`
   from a sibling migrated earlier without that knowledge. Mitigated by rule 10; fully
   solved only by planning module interfaces up front (Phase-5-era improvement).
3. *Flask-RESTX marshalling* (taxonomy #7 — expected-hard): namespaces + `@marshal_with`
   have no mechanical FastAPI equivalent; migration completes tasks but the suite can't
   collect. Standing red entry — documented, not hidden.
4. *Ops*: a wedged code-review-graph MCP call livelocked a job (fresh heartbeat = no
   rescue). Fixed: `CRG_TIMEOUT_SECONDS` + graceful no-graph degradation; also the
   deepest-frame recovery heuristic had to exclude pytest's trailing summary section.

## Sandbox accommodations (honest-oracle preserving)

These stand in for the repo's own documented dev setup, never for test logic:
- repo root on PYTHONPATH (≙ `pip install -e .`),
- `test_args` scoping (≙ the repo's own CI test selection; excludes Selenium/load tests),
- `test_env` (≙ the repo's documented test-env vars, e.g. TEST_DATABASE_URI → sqlite),
- `PORTAGE_TEST_SETUP` via test_env (≙ the repo's documented "provision test DB" step).
