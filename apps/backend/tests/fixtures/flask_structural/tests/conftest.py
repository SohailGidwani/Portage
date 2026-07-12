"""Framework plumbing; behavioural tests below remain framework-neutral."""

import pytest

from structapp import create_app
from structapp.db import get_db, init_db


@pytest.fixture
def app(tmp_path):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "test.sqlite")})
    with app.app_context():
        init_db()
        get_db().execute("INSERT INTO item (name) VALUES (?)", ("alpha",))
        get_db().commit()
    yield app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def runner(app):
    return app.test_cli_runner()
