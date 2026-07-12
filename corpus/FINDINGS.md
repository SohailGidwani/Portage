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
7. **Cross-file call-shape drift** — MITIGATED by R1, but the gate is **not closed**.
   The plan-time manifest now freezes binding-aware exports, original shape facts, call-site
   examples, and recipe pin decisions; dependency/SCC ordering and every generation/repair
   consume that same checkpointed artifact. The pre-sandbox checker caught three flaskr
   drafts that grew `get_db()` from zero required args to one. Two of five retained
   `tests/conftest.py` recovery diffs still changed the caller to `get_db(app)`, so caller
   compliance remains a Verify-time gap rather than a solved invariant. It was no longer
   the dominant terminal failure: all three flaskr gate runs instead converged on invented
   Flask-like context/CLI surfaces on `FastAPI` (`app.container`,
   `app.state.open_resource`, and related attributes).

   Task-7 measurements (2026-07-11): the Qwen inner loop (`r1-iter-final`, K=3) produced
   0/3 green for both flaskr and watchlist (avg test-pass 0.33 for each). The published
   GPT-4o grid (`r1-gate-final`, K=3) was:

   | repo | green | avg test-pass | avg completion | avg recover | avg cost | avg wall |
   |---|---:|---:|---:|---:|---:|---:|
   | flask-items-fixture | **3/3** | 1.00 | 1.00 | 0.0 | $0.025 | 11s |
   | minimal-flask-api | **2/3** | 0.67 | 0.89 | 2.0 | $0.033 | 16s |
   | flaskr | **0/3** | 1.00 | 0.00 | 3.0 | $0.387 | 75s |
   | watchlist | **0/3** | 1.00 | 0.00 | 3.0 | $0.255 | 52s |

   Verdict: fixture, minimal-flask-api, and watchlist's partial-credit threshold pass;
   flaskr's required ≥1/3 green fails. The 1.00 structural test-pass scores are rollback
   results, not successful migrations (completion is 0.00), so R1 stays unticked. The
   remaining dominant boundary is framework-inspecting test/setup migration plus caller-
   side enforcement, not loss of the manifest itself.

   **R1.1 follow-on (2026-07-11, GPT-4o):** implemented without corpus-path/test-name
   rules: frozen consumer bindings + direct-call arity/keyword checks; deterministic
   framework-seam decisions; bounded resource/factory/test-harness cluster generation
   (including coordinated retries); mechanical rejection of invented FastAPI capabilities;
   exact Click runner-result semantics; package re-export disambiguation; and restoration
   of full recipe subtask instructions in Execute. Added a bundled generic structural
   fixture (factory + `g/current_app` SQLite + Click + blueprint + pytest wiring).

   | suite/repo | green | avg test-pass | avg completion | avg recover | avg cost |
   |---|---:|---:|---:|---:|---:|
   | `r1-1-structural-confirm` / structural fixture | **3/3** | 1.00 | 1.00 | 1.0 | $0.044 |
   | `r1-1-final-gate` / items fixture | **3/3** | 1.00 | 1.00 | 0.0 | $0.021 |
   | `r1-1-final-gate` / minimal-flask-api | **3/3** | 1.00 | 1.00 | 1.7 | $0.025 |
   | `r1-1-flaskr-confirm` / flaskr | **0/3** | 1.00 | 0.00 | 3.0 | $0.250 |
   | `r1-1-final-gate` / watchlist | **0/3** | 1.00 | 0.00 | 4.0 | $0.291 |

   R1.1 establishes a reproducible structural seam and removes the minimal API's false
   `run` contract (`from api import app` was a package object re-export, not a submodule).
   It still does not close R1: flaskr/watchlist remain rollback-red with no residual
   mechanical contract violations, which isolates the next capability to deeper
   framework-inspecting setup, templates/sessions/auth, and Flask-coupled extensions.
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

## R2/R3 compatibility-first baseline — final GPT-4o grid (2026-07-12)

Suite `r2-r3-baseline-gpt4o-20260712-v2` ran the final rebuilt worker across all seven
development-corpus entries at K=3 (21 real jobs). The earlier suite without `-v2` was
aborted after detecting that its container predated the final crash-batch patch; its rows
remain diagnostic only and are not used below.

| repo | green | test-pass | completion | oracle | avg recovery | total cost |
|---|---:|---:|---:|---:|---:|---:|
| flask-items-fixture | **3/3** | 1.00 | 1.00 | 1.00 | 0.00 | $0.0375 |
| flask-structural-fixture | **3/3** | 1.00 | 1.00 | 1.00 | 0.67 | $0.1180 |
| minimal-flask-api | **0/3** | 1.00 | 0.25 | 1.00 | 3.00 | $0.0379 |
| flaskr | **0/3** | 1.00 | 0.1429 | 1.00 | 3.00 | $0.5277 |
| watchlist | **0/3** | 1.00 | 0.1667 | 1.00 | 3.00 | $0.1170 |
| microblog | **0/3** | 1.00 | 0.0526 | 1.00 | 3.00 | $6.0604 |
| flask-restx-api | **0/3** | 1.00 | 0.25 | 1.00 | 3.00 | $0.0914 |

Aggregate: **6/21 green (28.6%)**, $6.9898, 1,108.4 worker-seconds. Every run retained
100% final test pass and 100% oracle integrity. The external-app reds are therefore honest
completion failures followed by rollback, not weakened tests or false greens.

The grid identifies the next general boundary more sharply than another prompt iteration:

1. **A batch must be an executable migration cut, not merely the first file with an
   affected test.** Minimal API and watchlist migrated an `APIRouter` while their still-Flask
   app factory called `register_blueprint`; the first batch could not possibly pass. The
   planner must include required consumers/factory wiring (or a deterministic bridge) before
   Verify, using import/dependency structure rather than repository names.
2. **Whole-component crash recovery is too expensive for large graphs.** Microblog reset
   18 files three times for one circular-import collection failure: ~66.7 LLM calls/run and
   ~$2.02/run, then hit the $2 ceiling. Coherence must be preserved with SCC-aware blame or
   bounded batch bisection, not full-component blind regeneration.
3. **Extension profiles need early, explicit handling.** RESTX repeatedly tried to pass a
   Flask-RESTX `Namespace` to `APIRouter.include_router`; this should become a dedicated
   namespace conversion profile or an immediate `unsupported` result. Flask-SQLAlchemy/login
   and template/session apps need the same profile contract.

The immediate engineering order is therefore: executable-cut batching → bounded
SCC/component recovery → explicit idiom profiles/unsupported diagnosis → rerun baseline
K=3. Do not spend on the 105-job fault matrix until the external baseline completion rate
meaningfully improves.
