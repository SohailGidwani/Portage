"""Flask application factory + JSON error handlers.

The factory pattern (``create_app``) and the ``errorhandler`` registrations are exactly the
kind of framework wiring a Flask→FastAPI migration has to rewrite: the factory becomes a
``FastAPI()`` app that ``include_router``s the migrated router, and each error handler
becomes an ``exception_handler`` returning a ``JSONResponse``.
"""

from __future__ import annotations

from flask import Flask, jsonify

from .api import bp
from .store import InvalidItem, ItemNotFound


def create_app() -> Flask:
    app = Flask(__name__)
    app.register_blueprint(bp)

    @app.errorhandler(ItemNotFound)
    def _handle_not_found(exc: ItemNotFound):
        return jsonify({"error": str(exc)}), 404

    @app.errorhandler(InvalidItem)
    def _handle_invalid(exc: InvalidItem):
        return jsonify({"error": str(exc)}), 400

    return app
