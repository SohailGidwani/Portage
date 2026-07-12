"""Application factory for the generic structural Flask migration fixture."""

import os

from flask import Flask


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(DATABASE=os.path.join(app.instance_path, "items.sqlite"))
    if test_config is not None:
        app.config.update(test_config)

    os.makedirs(app.instance_path, exist_ok=True)

    from . import db
    from .views import bp

    db.init_app(app)
    app.register_blueprint(bp)
    return app
