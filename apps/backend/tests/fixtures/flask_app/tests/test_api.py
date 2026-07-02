"""Behavioural tests for the items API.

These assertions describe *what the API does*, not *how it is built*. They reach the app only
through the ``client`` and ``body`` fixtures (see conftest.py), so the very same file is the
oracle for the Flask app and, after migration, the FastAPI app. If these pass post-migration,
the behaviour was preserved.
"""


def test_health(client, body):
    r = client.get("/health")
    assert r.status_code == 200
    assert body(r) == {"status": "ok"}


def test_create_and_fetch_item(client, body):
    r = client.post("/items", json={"name": "buy milk"})
    assert r.status_code == 201
    item = body(r)
    assert item["name"] == "buy milk"
    assert item["done"] is False
    iid = item["id"]

    r2 = client.get(f"/items/{iid}")
    assert r2.status_code == 200
    assert body(r2) == {"id": iid, "name": "buy milk", "done": False}


def test_list_and_filter_by_done(client, body):
    client.post("/items", json={"name": "a"})
    r = client.post("/items", json={"name": "b"})
    bid = body(r)["id"]
    client.patch(f"/items/{bid}", json={"done": True})

    all_items = client.get("/items")
    assert all_items.status_code == 200
    assert len(body(all_items)) == 2

    done_items = client.get("/items?done=true")
    assert [i["name"] for i in body(done_items)] == ["b"]


def test_missing_item_returns_404(client, body):
    r = client.get("/items/999")
    assert r.status_code == 404
    assert "error" in body(r)


def test_create_rejects_empty_name(client, body):
    r = client.post("/items", json={"name": "   "})
    assert r.status_code == 400
    assert "error" in body(r)


def test_delete_item(client, body):
    r = client.post("/items", json={"name": "x"})
    iid = body(r)["id"]

    d = client.delete(f"/items/{iid}")
    assert d.status_code == 204

    assert client.get(f"/items/{iid}").status_code == 404
