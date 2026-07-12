"""A blueprint consuming the cross-file database helper."""

from flask import Blueprint, jsonify

from .db import get_db

bp = Blueprint("items", __name__)


@bp.route("/items")
def items():
    rows = get_db().execute("SELECT id, name FROM item ORDER BY id").fetchall()
    return jsonify([{"id": row["id"], "name": row["name"]} for row in rows])
