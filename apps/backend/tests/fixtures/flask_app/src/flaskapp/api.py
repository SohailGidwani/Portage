"""HTTP routes for the items API, as a Flask blueprint.

Exercises the parts of Flask a real migration must understand: a blueprint, multiple HTTP
methods, an int path converter, query-string parsing (``?done=true``), JSON body parsing,
and a non-200 status code. The Flask→FastAPI migration turns this blueprint into an
``APIRouter`` and these view functions into typed endpoints.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from . import store

bp = Blueprint("api", __name__)


@bp.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@bp.route("/items", methods=["GET"])
def list_items():
    done_arg = request.args.get("done")
    done = None if done_arg is None else done_arg.lower() == "true"
    return jsonify(store.list_items(done=done))


@bp.route("/items", methods=["POST"])
def create_item():
    payload = request.get_json(silent=True) or {}
    item = store.create_item(payload.get("name", ""))
    return jsonify(item), 201


@bp.route("/items/<int:item_id>", methods=["GET"])
def get_item(item_id: int):
    return jsonify(store.get_item(item_id))


@bp.route("/items/<int:item_id>", methods=["PATCH"])
def update_item(item_id: int):
    payload = request.get_json(silent=True) or {}
    item = store.update_item(item_id, name=payload.get("name"), done=payload.get("done"))
    return jsonify(item)


@bp.route("/items/<int:item_id>", methods=["DELETE"])
def delete_item(item_id: int):
    store.delete_item(item_id)
    return "", 204
