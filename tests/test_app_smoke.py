"""Smoke tests for the Postgres-only application.

Replaces tests/test_customers_devices.py, which posted to /api/mongo/* --
routes served by a MongoDB router that was disabled in main.py and had in
fact stopped importing at all (it referenced CustomerBase, a schema removed
during the Postgres migration). Those tests asserted 200 against endpoints
that could only ever 404, and one of them posted a device with no
customerId, which the handler rejects outright. They could not pass.

These check things that are actually true of the app as it ships.
"""

import pytest


def test_root_reports_ok(client):
    res = client.get("/")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert "Postgres" in body["app"]


def test_openapi_schema_builds(client):
    """Catches route/response-model errors that only surface on generation."""
    res = client.get("/openapi.json")
    assert res.status_code == 200
    assert res.json()["info"]["title"] == "FastAPI E-Commerce Backend"


def test_no_mongo_routes_remain(client):
    """The app is Postgres-only; /api/mongo/* must not come back."""
    paths = client.get("/openapi.json").json()["paths"]
    assert not [p for p in paths if p.startswith("/api/mongo")]


def test_expected_routers_are_mounted(client):
    paths = client.get("/openapi.json").json()["paths"]
    for prefix in ("/auth", "/trading"):
        assert any(p.startswith(prefix) for p in paths), f"no routes under {prefix}"


@pytest.mark.parametrize("path", ["/trading/position", "/trading/history"])
def test_trading_endpoints_respond(client, db_available, path):
    """The trading endpoints answer without raising.

    404 is a legitimate answer for /trading/position when nothing is open,
    so this asserts the app handled the request rather than a specific code.
    """
    if not db_available:
        pytest.skip("Postgres unreachable")
    assert client.get(path).status_code < 500
