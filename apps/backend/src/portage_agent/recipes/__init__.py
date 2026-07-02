"""Migration recipes (pluggable). v1: ``flask_to_fastapi``.

Importing this package registers the built-in recipes. The graph looks them up by name via
``get_recipe`` — an unknown name returns None, which the Plan node treats as "no migration"
(degrade to ingest→verify→report).
"""

from .base import PlannedFile, Recipe, Subtask, get_recipe, known_recipes, register
from .flask_to_fastapi import FlaskToFastAPIRecipe

__all__ = [
    "Recipe",
    "PlannedFile",
    "Subtask",
    "get_recipe",
    "known_recipes",
    "register",
    "FlaskToFastAPIRecipe",
]
