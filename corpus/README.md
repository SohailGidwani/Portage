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

- [x] `flask-items-fixture` — bundled, offline-clean (the Phase-2 fixture).
- [ ] 10–15 curated OSS repos — **collection in progress** (the Phase-4 long pole).
