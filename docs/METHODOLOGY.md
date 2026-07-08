# How Portage measures itself

The claim "an autonomous agent migrated this codebase" is easy to make and easy to fake.
This document explains how Portage's numbers are produced, what they mean, and — just as
deliberately — what they do *not* mean. The companion document is
[`../corpus/FINDINGS.md`](../corpus/FINDINGS.md), the failure taxonomy those numbers feed.

## 1. The oracle: the repo's own test suite

Every corpus repo ships a behavioural pytest suite that is **green on the unmodified
repo** (verified during corpus admission, in the same sandbox the eval uses). That suite
is the oracle: after migration, the *same assertions* must pass against the migrated app.

To keep the oracle honest, the bundled fixture (and the corpus curation criteria) push
framework coupling into a thin harness seam: tests reach the app through a `client`
fixture and a `body()` helper in `conftest.py`. The migration may rewrite that *plumbing*
(`app.test_client()` → `TestClient(app)`, `resp.get_json()` → `resp.json()`), but
behavioural assertions must keep their exact meaning — the recipe's harness rule forbids
deleting or weakening a test. A migration that only passes because the tests got easier
is a failed migration.

## 2. The honesty bar: what "green" means

A run scores **green** only if *all three* hold:

1. the full test suite passes (not just the blast-radius subset used during iteration),
2. **every** planned task completed, and
3. **zero** tasks were rolled back/skipped by recovery.

This bar exists because of a bug we caught live: recovery's skip-and-continue can roll
the entire worktree back to original sources — whose suite then passes, because it's the
original app. Early in Phase 4 such a run briefly scored "GREEN 24/24" with an empty
diff. The fix is structural now (the report reloads task truth from Postgres; the diff is
always recomputed; the harness requires full completion), so **the eval cannot be gamed
by giving up**. The CLI's exit code and the dashboard verdict use the same bar.

## 3. Statistical shape: K runs, mean ± variance, pinned everything

LLM migration is nondeterministic, so single runs are anecdotes. The harness runs every
(corpus repo × scenario) cell **K times** (K=3 for the headline grid) and persists
per-run rows (`runs`) plus mean±variance aggregates (`metrics`). Variance is reported,
not smoothed away — e.g. `minimal-flask-api` baseline is 2/3 green with test-pass
variance 0.33: that 1-in-3 organic flake is a *finding* (it motivated the export-contract
mechanism), not noise to hide.

Reproducibility pins: every remote corpus repo is pinned to a commit SHA (the manifest
rejects unpinned remotes); the sandbox image pins every dependency; the model and its
config are recorded per attempt in `attempts_log`.

## 4. Fault injection: recovery is measured, not narrated

Three deterministic faults are standing eval scenarios (promoted from the Phase-3 DoD):

| fault | what it simulates | expected rescue |
|---|---|---|
| `bad_patch` | a corrupted first migration attempt | crash classification → targeted rollback of the implicated file → regenerate |
| `bad_patch_until_escalation` | a task the driver tier keeps failing | escalation tier takes over (every attempt's tier/model recorded — "how often does escalation rescue?" is a query) |
| `drop_task` | a planner miss (a file left un-migrated) | residue detection → replan appends the missing task |

Recovery quality is read as a **delta against the same repo's baseline** green rate —
injected faults stack on organic flake, so a single averaged "recovery rate" would
flatter easy repos and slander hard ones. On the stable tier the deltas are zero:
3/3 green under both fault scenarios. Crash-resume (kill the worker mid-run) is
covered separately by `scripts/dod_check.sh` since the harness can't SIGKILL the worker
it depends on.

## 5. The corpus: pinned, tiered, honestly curated

Six repos across four difficulty tiers (baseline JSON APIs → structural template apps →
framework-extension apps → heavy multi-extension apps), each admitted only after its
suite ran green in the offline sandbox at the pinned SHA. Curation criteria, the vetting
log, and — importantly — the **dropped candidates with reasons** are in
[`../corpus/README.md`](../corpus/README.md). Four candidates fell to one shared cause
(mutually incompatible dependency pins can't share one sandbox image); that admission
constraint is documented as a finding with its unlock (per-repo sandbox images) rather
than silently shrinking the corpus.

Sandbox accommodations (PYTHONPATH for manifest-less repos, `test_args` scoping to
exclude Selenium/load tests, `test_env`, a schema-provision hook) each stand in for the
repo's *own documented dev setup* — never for test logic.

## 6. Cost is measured, not estimated

Every LLM call's tokens and USD cost are recorded per attempt (via LiteLLM pricing),
summed per job (`llm_usage`), and averaged per cell in the grid. Retries and escalations
are *included* — the $1.50/run on the hardest repo is mostly recovery rounds, and that
cost-scales-with-recovery relationship is part of the result.

## 7. What these numbers do NOT show

- **Generality.** One recipe (Flask→FastAPI), one language, six repos. The architecture
  is recipe-agnostic; the *evidence* is recipe-specific by design (narrow + measured).
- **Stronger-model lift.** Driver and escalation tier currently resolve to the same
  model (GPT-4o), so escalation-rescue numbers measure the *retry-ladder machinery*
  (enriched-context retry), not a stronger model's lift. Swapping a real second model in
  is an env change; the measurement is designed for it.
- **Big-repo behaviour.** Corpus repos are ≤ ~40 files. Long-horizon behaviour on
  thousand-file repos is unproven (test-impact analysis and parallel sandboxes are the
  planned levers).
- **Immunity to prompt-tuning bias.** Several recipe rules were added after observing
  corpus failures; the grid therefore partly measures a recipe tuned to this corpus.
  This is disclosed rather than pretended away — it's how the recipe is *supposed* to
  improve — and the fix directions in FINDINGS name which failures remain structural.

## 8. Reproduce it

```bash
docker compose up -d && docker compose --profile tools build sandbox
# the headline grid (18 runs, ~$3 with GPT-4o):
docker compose run --rm worker python -m portage_agent.eval \
  --corpus /corpus/corpus.toml --k 3 --scenarios baseline --suite repro-$(date +%s)
# fault scenarios on the stable tier:
docker compose run --rm worker python -m portage_agent.eval \
  --corpus /corpus/corpus.toml --k 3 --scenarios bad_patch,bad_patch_until_escalation \
  --repos flask-items-fixture,minimal-flask-api --suite repro-faults-$(date +%s)
```

Results land in the `runs`/`metrics` tables and render on the dashboard's
[`/eval`](http://localhost:3000/eval) proof page. The per-phase DoD scripts
(`scripts/dod_check.sh`, `phase{1,2,3}_check.sh`, `phase4_smoke.sh`) are re-runnable
assertions of every claim above.
