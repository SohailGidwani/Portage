# Portage eval findings — failure taxonomy & reliability (Flask → FastAPI, v1)

The Phase-4 "known limitations" report (plan v2 §15): what the autonomous migration
handles reliably, what breaks, and *why* — with the evidence. A results table that includes
analyzed failures is the point; an all-green sheet would mean the corpus is too easy.

Status: **this report closes Phase 4** (decision 2026-07-08): the corpus ships at 6 pinned
repos across 4 tiers — the original ≥10 target was traded for the documented
shared-sandbox dependency-conflict finding (last section), which blocks the dropped
candidates and defines the unlock (per-repo sandbox images).

Methodology: every number comes from the `runs`/`metrics` tables (nothing hand-collected);
K runs per (repo × scenario) with mean±variance; **green = full suite passes AND every
planned task completed AND none rolled back** — a run that recovery rolls back to original
sources passes its own suite and is scored red (see "integrity" below). Corpus pinned by
SHA in `corpus.toml`; driver model: GPT-4o (Azure), escalation tier enabled.

## Headline (suite `k3-baseline`, K=3, 6 repos — 2026-07-08)

| repo | tier | green | avg test-pass | avg recover visits | avg cost | avg wall |
|---|---|---|---|---|---|---|
| flask-items-fixture | baseline | **3/3** | 1.00 | 0.0 | $0.022 | 10s |
| minimal-flask-api | baseline | **2/3** | 0.67 | 0.3 | $0.013 | 10s |
| flask-restx-api | framework | 1/3 | 0.67 | 3.3 | $0.044 | 17s |
| flaskr | structural | 0/3 | 0.67 | 3.7 | $0.250 | 55s |
| watchlist | structural | 0/3 | 0.67 | 4.0 | $0.261 | 61s |
| microblog | heavy | 0/3 | 0.00 | 4.3 | $1.503 | 165s |

(green = full migration + full suite; avg test-pass counts partial credit — the 0.67 rows
mean most tests pass but green's all-or-nothing bar isn't met.)

**The reliability boundary is idiom, not size.** JSON APIs (routes + parsing + error
handlers, the fixture and minimal-flask-api) migrate green with zero recovery at ~$0.01–0.02
per repo. Server-rendered apps (templates + sessions + auth + a DB seam) complete their
task DAGs but fail behaviorally; the wall is specific and named below.

## Fault recovery (suite `k3-faults`, K=3, stable tier)

| repo | scenario | green | avg recover visits | avg cost |
|---|---|---|---|---|
| flask-items-fixture | bad_patch | **3/3** | 1.0 | $0.029 |
| flask-items-fixture | bad_patch_until_escalation | **3/3** | 2.0 | $0.054 |
| minimal-flask-api | bad_patch | 1/3 | 1.3 | $0.020 |
| minimal-flask-api | bad_patch_until_escalation | 0/3 | 2.0 | $0.027 |

Fault recovery is **100% on the fixture** (targeted rollback+regenerate; escalation-tier
rescue fires and is logged per attempt) but degrades on the real repo: minimal-flask-api's
baseline already flakes 1-in-3, and an injected fault stacks on that base rate — corrupting
the first file consumes retry budget that the repo's organic flake then needs. Honest
implication: fault-recovery rate is only meaningful relative to baseline reliability, so
the headline metric should be reported per-repo as (fault green − baseline green) deltas,
not a single average.

## Failure taxonomy (ordered easy → hard; each entry names its fix or its status)

1. **Routing / parsing / responses / error handlers** — SOLVED by recipe rules. Notable
   pitfalls encoded after observed failures: `JSONResponse` wrapping silently overriding
   `status_code` (201→200); `HTTPException` bypassing app-level handlers (`{"detail"}` vs
   `{"error"}` bodies); `redirect()` must become `RedirectResponse(..., 302)` — the 307
   default re-sends POST bodies.
2. **Cross-file name contracts** — SOLVED structurally: an AST pass computes each file's
   export contract (names siblings import) and states it in the prompt. Before: ~50%
   flake on a 3-file app (dropped `router` export).
3. **Deprecated/hallucinated APIs** — SOLVED by rules 11/12 after observed failures:
   `@app.on_event` (warning-as-error under pytest), invented packages (`fastapi_flash`,
   `fastapi_login`). Class remains open-ended; each instance is cheap to encode once seen.
4. **Environment gaps** — SOLVED case-by-case, pinned in the sandbox image: FastAPI's
   `Form()` silently requires `python-multipart` (took watchlist from collection-crash to
   all 15 tests executing).
5. **App factory & config** — MOSTLY SOLVED: `app.config`-as-plain-dict pattern (the model
   invented `State.update()`), instance-path handling, lifespan-not-on_event. Residual
   factory bugs cluster in rarely-exercised branches (test_config vs instance config).
6. **Templates / sessions / flash / auth** — PARTIAL: with Jinja2Templates + Session-
   Middleware + session-auth guidance, template apps now complete their DAGs, their
   suites *execute*, and most tests pass (flaskr and watchlist both average 0.67
   test-pass at K=3) — but the all-or-nothing green bar isn't met. The v1 frontier.
7. **Cross-file call-shape drift** — OPEN, the dominant residual failure: multi-file
   regeneration keeps *signatures* coherent nowhere — flaskr's `get_db()` drifts between
   "plain function", "needs request", "context manager" across files/attempts (19/24
   failures in the final probe). The export contract pins names, not call shapes. Fix
   direction: extend the contract pass to include signatures/usage snippets of imported
   symbols, or plan a shared "interfaces" step before per-file migration.
8. **Flask-coupled extensions** (`flask_sqlalchemy`, `flask_restx`) — OPEN / v1 boundary,
   now partially cracked: with the plain-SQLAlchemy and RESTX guidance plus self-review
   retries, **flask-restx-api reached green 1/3 at K=3** (was: never collected). The
   flask_sqlalchemy path (watchlist) completes but fails behaviorally. Needs per-extension
   sub-strategies to become reliable; measured honestly meanwhile.
9. **Framework-inspecting tests** — OPEN by design: tests asserting on `flask.session`/
   `g`/`app.testing` internals can't pass unchanged against FastAPI; the harness rule
   rewrites plumbing while preserving assertion *meaning*, which works for client/body
   seams but not for deep framework introspection (flaskr's `test_factory.py::test_config`).

## Recovery & integrity findings (the durability story, measured)

- **Deepest-frame blame + widen-on-repeat**: crash tracebacks implicate the deepest
  planned file; when the same lone file is implicated twice running, single-file blame
  isn't converging (the crash *site* isn't the *offender* — observed: a migrated test
  calling `create_app()` at import burned the factory's budget) and recovery widens to a
  full reset. Demonstrated rescuing flaskr mid-run.
- **Retry-with-self-review**: rolled-back attempts keep their failing diff, and retries
  see it ("debug your own code") instead of regenerating blind. Effect: flaskr factory
  went from exhausted-and-skipped (3 blind attempts) to completing all 6 tasks.
- **False-green integrity**: skip-and-continue can roll the whole worktree back; the
  original suite then passes. Caught live (a "GREEN 24/24" with an empty diff): now the
  report reloads task truth from Postgres, the diff is always recomputed, and green
  requires full completion. The eval cannot be gamed by giving up.
- **Skip-out false pass** (found 2026-07-09, fixed): a model migrated a test file by
  decorating every test with skip — pytest reported total>0, failed=0, errors=0, and
  Verify's predicate called that a PASS, so recovery never fired (the strict harness
  green bar still scored the run red, but recovery deserved its chance). Verify now
  additionally requires **passed > 0**: an all-skipped suite is a failure to recover
  from, never a pass. Regression: baseline + fault + replan scenarios re-run green.
- **Escalation, measured**: with driver == escalation model (same GPT-4o deployment),
  escalation-rescue rates measure the *retry ladder machinery*, not stronger-model lift —
  swap `LLM_ESCALATION_MODEL` to a stronger deployment to measure real lift (env-only).
- **Two-model escalation lift, MEASURED (2026-07-09, suites `qwen-ladder` vs
  `qwen-control`)**: driver = Qwen3 Coder Next (Ollama cloud) in both arms; escalation =
  GPT-4o (ladder) vs Qwen itself (control). Same grid: fixture + minimal-flask-api
  baselines and fixture `bad_patch_until_escalation`, K=3 each.

  | arm | green | fault-scenario green | total cost | avg wall |
  |---|---|---|---|---|
  | ladder (qwen → GPT-4o) | **8/9** | **3/3** | $0.093 | 39s |
  | control (qwen → qwen) | 5/9 | 1/3 | $0.00* | 78s |

  The stronger escalation tier is worth **+3 greens of 9** under identical conditions —
  concentrated exactly where it should be: the injected-fault scenario (3/3 vs 1/3) and
  a fixture baseline the weak driver couldn't converge alone. The ladder also halves
  wall time (GPT-4o resolves in fewer attempts than qwen burning full budgets). Bonus
  integrity demo: one control run went red with the suite at 6/6 — all three tasks
  exhausted and rolled back, the ORIGINAL suite passed, and the honest-green bar refused
  it. (*Ollama-hosted models are absent from LiteLLM's price map, so their calls cost
  $0.00 in the ledger — the ladder arm's $0.093 is the GPT-4o share only. That
  cost-blindness is also why the hosted demo keeps GPT-4o: the demo-protection spend
  caps cannot see unpriceable models.)

## Corpus admission findings

Four candidates were dropped for one shared reason worth naming: **a single shared sandbox
image cannot serve mutually incompatible dependency pins** (2017-era Flask 0.12 stacks,
abandoned `flask_restplus`, `itsdangerous<2.1`, SQLAlchemy 1.x APIs). Per-repo sandbox
images are the documented unlock if corpus breadth becomes the goal.
