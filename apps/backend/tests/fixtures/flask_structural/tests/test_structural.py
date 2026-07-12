"""Behavioural oracle for the generic resource/factory/test-harness seam."""


def test_items_use_initialized_database(client):
    response = client.get("/items")
    assert response.status_code == 200
    assert response.get_json() == [{"id": 1, "name": "alpha"}]


def test_database_cli_is_preserved(runner):
    result = runner.invoke(args=["init-db"])
    assert result.exit_code == 0
    assert "Initialized the database." in result.output
