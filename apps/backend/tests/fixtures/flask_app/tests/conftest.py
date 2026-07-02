"""Test harness wiring — the only framework-coupled test code.

The ``client`` and ``body`` fixtures are the seam between the test *behaviour* (in
``test_api.py``, which never changes) and the web framework. The Flask→FastAPI migration
rewrites THIS file — ``app.test_client()`` → ``TestClient(app)`` and ``resp.get_json()`` →
``resp.json()`` — so the behavioural assertions stay byte-for-byte identical before and
after. That is what makes the test suite an honest migration oracle: same assertions, new
framework.
"""

import pytest

from flaskapp import store
from flaskapp.app import create_app


@pytest.fixture
def client():
    store.reset()
    app = create_app()
    return app.test_client()


@pytest.fixture
def body():
    """Return a helper that extracts the JSON body from a response.

    Flask responses expose ``.get_json()``; FastAPI/Starlette responses expose ``.json()``.
    Keeping that difference here (not in the tests) is what lets test_api.py stay unchanged.
    """

    def _body(resp):
        return resp.get_json()

    return _body
