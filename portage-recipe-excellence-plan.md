# Portage — Recipe Excellence Plan (Phase R)

**Decision (Sohail, 2026-07-09):** deployment is PARKED. Depth before breadth, per advisor
review: *improve one recipe deeply → validate on held-out repos → harden public execution →
launch → only then recipe #2.* The P8 runbook (`portage-p8-deployment-runbook.md`) stays
valid for when hosting resumes; the repo is already deploy-ready (commit 9bca418).

**The thesis this phase must prove:** "Portage reliably performs one genuinely difficult
migration, measures where it fails, and recovers better than a generic coding agent."

## Where we are (measured, suites k3-baseline / k3-faults, GPT-4o driver)

| tier | repos | green | avg test-pass |
|---|---|---|---|
| JSON API | fixture, minimal-flask-api | 3/3, 2/3 | 1.00 / 0.67 |
| framework | flask-restx-api | 1/3 | 0.67 |
| template/structural | flaskr, watchlist | 0/3, 0/3 | 0.67 |
| heavy | microblog | 0/3 | 0.00 |

Dominant residual (FINDINGS §7): **cross-file call-shape drift** — 19/24 failures in the
final flaskr probe. Root cause found in code review (2026-07-09): role-based execution
order migrates routers *before* the support modules they import (router=10 < support=15),
so importers guess interfaces that their dependencies later contradict; the export
contract pins names a file must DEFINE but says nothing about shapes it must CALL; and
recovery's widen-reset regenerates everything with no pinned decisions to converge on.

## Readiness bar (from advisor review — the exit criteria for Phase R)

- JSON API tier: **≥90% green** across repeated runs (K≥3).
- Template/session tier: **≥70–80% green**.
- Extension-heavy tier: meaningfully supported **or rejected early** with an accurate
  "unsupported profile" diagnosis.
- Fault scenarios: **no meaningful degradation** from baseline.
- **No false greens, no weakened tests** (mechanically enforced, not just prompted).
- Results **reproduced on held-out repositories** never used during development.

Measurement discipline: the active driver is GPT-4o for both iteration and **gate** grids;
Qwen remains only a recorded historical experiment. Published claims use the platform
model. Gate suites are named `rN-gate` in the `runs` table.

## Stages

### R0 — Security hygiene (immediate, before any of the below)
- [ ] **Sohail rotates credentials**: Azure OpenAI key (portal → regenerate), Ollama key
      (dashboard → new key), GitHub OAuth client secret if it ever left `.env`. Then
      update `.env`. Rationale: local creds have circulated through consultations/tools.
- [ ] Strengthen `JWT_SECRET` in `.env` (`openssl rand -hex 32`).
- [ ] Confirm nothing is publicly reachable: loopback-only port bindings (already true,
      commit 9bca418), no hosted deployment, `AUTH_MODE` per preference locally.
- [ ] Keep running only corpus/pinned repos locally ("avoid running repositories you do
      not trust" — the DooD socket + shared volume hardening lands before launch, not now).

### R1 — Cross-file interface consistency  ← IMPLEMENTED; GATE NOT CLOSED

Status (2026-07-11): Tasks 1–7 were executed. `r1-gate-final` retained the fixture at
3/3 and minimal-flask-api at 2/3; watchlist averaged 1.00 test-pass, but flaskr remained
0/3 green because all planned files rolled back. R1 therefore stays open; measured details
and the new failure boundary are in `corpus/FINDINGS.md` §7.

R1.1 follow-on is also implemented generically: caller binding/arity enforcement,
checkpointed framework-seam decisions, bounded coordinated seam generation/retries, and a
new bundled structural fixture. Confirmation: structural fixture 3/3, original fixture
3/3, minimal-flask-api 3/3; flaskr and watchlist remain 0/3 completion-green. R1 stays
open for the deeper framework-inspection/template/session/extension layer.

Attacks the dominant failure. The detailed task plan is local-only:
`notes/2026-07-09-r1-interface-consistency-design.md`
1. Import-graph **topological execution order** (dependencies first; role order as
   tiebreak; cycle-safe). Fixes db-after-routers.
2. **Interface contracts v2**: export contract grows signatures + call-site examples +
   lifecycle notes (generator/context-manager/decorators), extracted by AST.
3. Prompts state both sides: **DEFINES** (what this file must keep exporting, with
   shapes) and **CALLS** (the shapes this file must invoke, anchored to already-migrated
   dependency source when available).
4. **Mechanical contract check** on every generated file (AST): missing exports trigger
   one immediate repair call (logged in attempts_log as `contract_repair`) before any
   sandbox run.
*Gate (`r1-gate`, GPT-4o, K=3): flaskr ≥1/3 green AND call-shape drift no longer the
dominant probe failure; watchlist test-pass ≥0.75; fixture 3/3 and mfa ≥2/3 unchanged.*

### R2 — Recovery completeness  ← IMPLEMENTED; BROADER CORPUS GATE PENDING
1. [x] **Integrate failures route back to Recover** (one reserved full-suite
   recovery cycle) instead of ending the run unrepaired — advisor's #3, confirmed in
   `graph.py`.
2. [x] Per-task/coupled-unit verification batches for precise blame — replaces forensic
   deepest-frame guessing with "the task that just ran"; sandbox-only cost.
3. [x] Exact failure+diff fingerprints trigger bounded diagnostic escalation and stop
   identical no-progress loops on the third occurrence.
*Local gate closed 2026-07-12: structural fixture job
`9c53ec0a-3cc4-4761-b534-881301f16af1` repaired an `integration_only` regression in one
Integrate recovery visit and finished 6/6 tasks, 2/2 tests. The K-grid corpus comparison
remains pending.*

### R3 — Protect the oracle  ← IMPLEMENTED; ORACLE GATE CLOSED
1. [x] Mechanical **oracle-integrity check** for test_harness migrations: same test function
   set, no lost/weakened assertions (per-function assert census), no introduced skips —
   violations fail the attempt (generalizes the skip-out fix from Verify-time to
   Execute-time).
2. [x] Prefer *not* migrating tests: a deterministic conftest-level compatibility facade for
   the test-client seam; measure how far it carries.
3. [x] Oracle-check results surfaced in report.json (the defensibility artifact), with
   explicit `success` / `failed` / `unsupported` migration outcomes.
*Gate closed 2026-07-12: adversarial unit checks catch deleted/changed assertions,
renamed tests, added decorator/runtime/module skips, changed raises/parametrize, and changed
fixture dependencies/lifecycle. Suite `r2-r3-baseline-gpt4o-20260712-v2` reports 100%
oracle integrity across all 21 development-corpus runs (green and rollback-red).*

### R4 — Explicit Flask idiom profiles
"Flask" is several migration classes. Formalize detection → **profile** (json_api /
templates_sessions / flask_login / flask_sqlalchemy / flask_restx — a repo can have
several), each with dedicated rules, validation expectations, sandbox deps; plus an
**unsupported-profile early exit**: detection that can't be served (flask_restplus,
2017-era pins) ends the run immediately with an honest `unsupported` diagnosis instead
of burning budgets.
*Gate: watchlist ≥2/3 green; restx ≥2/3 or clean unsupported diagnosis; dropped-corpus
repos produce the early diagnosis.*

### R5 — Held-out validation
Current 6 repos = **development corpus** (they've shaped rules since Phase 4; treat all
tuning as fit to them). Curate 3–5 fresh repos (start from the unused entries in
`portage-corpus-candidates.md`), vet with `scripts/vet_corpus_repo.sh`, pin SHAs,
**never run them during R1–R4**. Freeze the recipe, run the K=3 grid once, publish dev
vs held-out side by side (leaderboard suite selector already supports this).
*Gate: the readiness bar above, on held-out numbers.*

### Then (in order): input hardening for public execution (repo allowlisting/size caps,
per-job sandbox volumes, config allowlisting, SSRF protection, CLI auth) → un-park the
P8 runbook Part B → launch → recipe #2 (unittest→pytest) only after.

## Sequencing rationale
R1 before R2: better generation shrinks the recovery load; R2's precise blame then
covers what R1 misses. R3 before R4: idiom work adds more test-file rewriting, so the
oracle guard must exist first. R5 last and untouched until the recipe freezes —
otherwise it silently becomes a second dev set.
