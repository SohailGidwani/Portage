"""In-memory item store + domain logic.

Framework-agnostic on purpose: this module imports no web framework, so the Flask→FastAPI
migration leaves it untouched. The routes are a thin shell over these functions, which keeps
the migration focused on the framework seam (routing, request parsing, error mapping) and
gives the knowledge graph a stable callee that both the old and new routes depend on.
"""

from __future__ import annotations


class ItemNotFound(Exception):
    """Raised when an item id is not present. The web layer maps this to HTTP 404."""


class InvalidItem(Exception):
    """Raised when item data fails validation. The web layer maps this to HTTP 400."""


_items: dict[int, dict] = {}
_next_id: int = 1


def reset() -> None:
    """Clear the store. Tests call this between cases for isolation."""
    global _items, _next_id
    _items = {}
    _next_id = 1


def list_items(done: bool | None = None) -> list[dict]:
    """Return all items, optionally filtered by their ``done`` flag."""
    items = list(_items.values())
    if done is not None:
        items = [i for i in items if i["done"] is done]
    return items


def get_item(item_id: int) -> dict:
    """Return one item by id, or raise ItemNotFound."""
    try:
        return _items[item_id]
    except KeyError:
        raise ItemNotFound(f"item {item_id} not found") from None


def create_item(name: str) -> dict:
    """Validate + create an item; raise InvalidItem on an empty name."""
    name = (name or "").strip()
    if not name:
        raise InvalidItem("name must not be empty")
    global _next_id
    item = {"id": _next_id, "name": name, "done": False}
    _items[_next_id] = item
    _next_id += 1
    return item


def update_item(
    item_id: int, *, name: str | None = None, done: bool | None = None
) -> dict:
    """Partially update an item; raise ItemNotFound / InvalidItem as appropriate."""
    item = get_item(item_id)
    if name is not None:
        name = name.strip()
        if not name:
            raise InvalidItem("name must not be empty")
        item["name"] = name
    if done is not None:
        item["done"] = bool(done)
    return item


def delete_item(item_id: int) -> None:
    """Delete an item by id, or raise ItemNotFound."""
    if item_id not in _items:
        raise ItemNotFound(f"item {item_id} not found")
    del _items[item_id]
