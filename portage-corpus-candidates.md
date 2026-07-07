# Portage — Phase 4 Corpus Candidates (Flask → FastAPI)

Vetted via the GitHub API: each entry below was confirmed to (a) be a Flask app, (b) ship a
real test suite (the oracle), (c) carry a permissive license, (d) be tractable in size.
Difficulty tiers give the spread the failure taxonomy needs. Pin each `ref` to a commit SHA
at vendor time so K-runs are reproducible.

## Vetted corpus (10 confirmed)

| id | repo | license | size | py/tests | tier | stresses (taxonomy hooks) |
|---|---|---|---|---|---|---|
| flask-pytest-example | aaronjolson/flask-pytest-example | MIT | 3KB | 6/2 | baseline | routing, jsonify |
| todo-list | marcosvbras/todo-list-python | Apache-2.0 | 168KB | 3/1 | baseline | templates, form parsing |
| minimal-flask-api | markdouthwaite/minimal-flask-api | MIT | 20KB | 7/3 | baseline | json body, error handlers |
| flask-for-startups | nuvic/flask_for_startups | MIT | 82KB | 22/2 | structural | blueprints, app factory, services layer |
| flask-api-sdet | sdetAutomation/flask-api | MIT | 100KB | 26/7 | structural | schemas, integration tests, multiple resources |
| flask-restx-api | Anishmourya/flask-restx-api | MIT | 11KB | 8/3 | framework | Flask-RESTX namespaces + marshalling -> routers + Pydantic |
| flask-celery | matthieugouel/python-flask-celery-example | MIT | 45KB | 13/3 | framework | Celery tasks, async responses, app wiring |
| flasky | miguelgrinberg/flasky | MIT | 254KB | 39/6 | heavy | blueprints, factory, Flask-Login, SQLAlchemy, WTF forms |
| microblog | miguelgrinberg/microblog | MIT | 83KB | 34/1* | heavy | Flask-Login/Migrate/Mail/Babel, full-text search (tests in tests.py) |
| testing-goat | urangurang/flask-tdd-with-testing-goat | MIT | 315KB | 14/4 | caveated | unit tests OK; **strip Selenium functional tests** (won't run network-off) |

\* microblog's tests live in a single `tests.py` (unittest) — pytest won't auto-collect it;
use `test_cmd = "python -m unittest tests"` or `pytest tests.py`.

## To verify for margin (reach 13–15) — rate limit stopped automated checks

- `pallets/flask` -> `examples/tutorial` (Flaskr): the canonical app-factory + blueprint +
  pytest app, BSD. Vendored as a subdir. Gold-standard baseline; strongly recommend.
- `helloflask/watchlist` (Grey Li tutorial) — small, has tests, MIT (confirm).
- `greyli/todoism` — Flask todo with tests, MIT (confirm).
- `hackersandslackers/flask-sqlalchemy-tutorial` — blueprints + SQLAlchemy (confirm license/tests).

## Dropped (and why)

- `cburmeister/flask-bones` — 0 test files found → no oracle. (Nice blueprint structure, but
  unusable as a scored entry. Keep only as a layout reference.)
- `viniciuschiele/flask-rest-example`, `dunossauro/crudzin`, `melardev/FlaskApiEcommerce` —
  no detectable license → don't vendor.
- `dtiesling/flask-muck`, `cs91chris/flask_autocrud`, `vsdudakov/fastadmin` — frameworks/
  libraries, not apps.

## corpus.toml (align keys to your actual schema)

```toml
# Pin every ref to a commit SHA at vendor time. Set K >= 3 per entry.
# test_style drives whether the recipe must also migrate the tests (see README note).

[[repo]]
id         = "flask-pytest-example"
url        = "https://github.com/aaronjolson/flask-pytest-example"
ref        = "PIN_SHA"
subdir     = ""
test_cmd   = "pytest -q"
test_style = "flask_test_client"   # tests use app.test_client() -> must be migrated
tier       = "baseline"
stresses   = ["routing", "jsonify"]

[[repo]]
id         = "minimal-flask-api"
url        = "https://github.com/markdouthwaite/minimal-flask-api"
ref        = "PIN_SHA"
test_cmd   = "pytest -q tests/test_api.py"   # exclude the locust load test
test_style = "flask_test_client"
tier       = "baseline"
stresses   = ["json_body", "error_handlers"]

[[repo]]
id         = "todo-list"
url        = "https://github.com/marcosvbras/todo-list-python"
ref        = "PIN_SHA"
test_cmd   = "pytest -q"
test_style = "flask_test_client"
tier       = "baseline"
stresses   = ["form_parsing", "templates"]

[[repo]]
id         = "flask-for-startups"
url        = "https://github.com/nuvic/flask_for_startups"
ref        = "PIN_SHA"
test_cmd   = "pytest -q"
test_style = "flask_test_client"
tier       = "structural"
stresses   = ["blueprints", "app_factory", "services_layer"]

[[repo]]
id         = "flask-api-sdet"
url        = "https://github.com/sdetAutomation/flask-api"
ref        = "PIN_SHA"
test_cmd   = "pytest -q"
test_style = "flask_test_client"
tier       = "structural"
stresses   = ["schemas", "multiple_resources", "integration"]

[[repo]]
id         = "flask-restx-api"
url        = "https://github.com/Anishmourya/flask-restx-api"
ref        = "PIN_SHA"
test_cmd   = "pytest -q"
test_style = "flask_test_client"
tier       = "framework"
stresses   = ["flask_restx", "namespaces", "marshalling"]

[[repo]]
id         = "flask-celery"
url        = "https://github.com/matthieugouel/python-flask-celery-example"
ref        = "PIN_SHA"
test_cmd   = "pytest -q test/"
test_style = "flask_test_client"
tier       = "framework"
stresses   = ["celery", "async_responses", "app_wiring"]

[[repo]]
id         = "flasky"
url        = "https://github.com/miguelgrinberg/flasky"
ref        = "PIN_SHA"
test_cmd   = "pytest -q tests/"
test_style = "flask_test_client"
tier       = "heavy"
stresses   = ["flask_login", "sqlalchemy", "wtf_forms", "blueprints", "factory"]

[[repo]]
id         = "microblog"
url        = "https://github.com/miguelgrinberg/microblog"
ref        = "PIN_SHA"
test_cmd   = "python -m unittest tests"   # single tests.py, unittest style
test_style = "unittest_direct"            # instantiates app directly, not test_client
tier       = "heavy"
stresses   = ["flask_login", "flask_migrate", "flask_mail", "babel", "search"]

# caveated — include only after stripping the Selenium functional tests
[[repo]]
id         = "testing-goat"
url        = "https://github.com/urangurang/flask-tdd-with-testing-goat"
ref        = "PIN_SHA"
test_cmd   = "pytest -q blog/base blog/posts"   # unit tests only; exclude functional_test/
test_style = "flask_test_client"
tier       = "caveated"
stresses   = ["blueprints", "posts_crud"]
```

## Failure-taxonomy scaffold (the other half of the Phase 4 DoD)

Structure the write-up by *migration concern*, and for each record which corpus entries
exercised it, the pass rate, and the observed failure mode. Expected categories, ordered
roughly easy → hard:

1. **Routing** — `@app.route(methods=[...])` → `@router.get/post`; `<int:id>` converters →
   typed path params. (baseline tier)
2. **Request parsing** — `request.args/form/json/files` → query params, Pydantic bodies,
   `Form`, `UploadFile`. (baseline/structural)
3. **Responses** — `jsonify`, `(body, status)` tuples, `make_response` → return models /
   `JSONResponse` / `status_code`. (baseline)
4. **Error handling** — `@app.errorhandler` → `@app.exception_handler`. (baseline)
5. **Blueprints** — `Blueprint` → `APIRouter` + `include_router`. (structural)
6. **App factory & config** — `create_app`, `app.config` → app object + `pydantic-settings`,
   lifespan for startup/shutdown. (structural)
7. **Extensions with no FastAPI equivalent** — Flask-SQLAlchemy → plain SQLAlchemy/SQLModel;
   Flask-RESTX Resource/marshal → routers + Pydantic; Flask-WTF → Pydantic; Flask-Login →
   dependency-based auth. **This tier is where you expect real failures — document them.**
8. **Request context** — `g`, `before_request`/`after_request`, `current_app` → dependencies
   and middleware. (heavy)
9. **Test-harness migration** — `app.test_client()` → Starlette `TestClient`; `url_for` in
   tests. Track separately: a migration can be logically correct yet score 0 if the tests
   weren't ported.

A results table that reports *where and why it failed* on tiers 7–9 is more credible to a
technical interviewer than an all-green sheet. That honesty is the point.
