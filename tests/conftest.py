"""Shared test fixtures.

This used to patch a MongoDB layer that no longer exists -- the app is
Postgres-only. Nothing here mocks a database now: the tests that need one
skip when it is unreachable rather than asserting against a fake.
"""

import os
import sys

import pytest

# Repo root on sys.path so tests can import the application modules.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture(scope="session")
def client():
    """TestClient with the app's lifespan run, so startup work happens."""
    from fastapi.testclient import TestClient

    from main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def db_available() -> bool:
    """Whether Postgres is reachable, so DB-backed tests can skip cleanly."""
    try:
        from sqlalchemy import text

        from config.db_pgrs import engine
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
