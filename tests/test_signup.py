"""Public sign-up (2026-09-23): role 'user', email-verified, no enumeration.

Runs against an in-memory SQLite holding just the three tables involved, so
it needs neither Postgres nor mail.
"""

import re

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models_pgdb.models as models
from helpers import mailer
from helpers.auth_deps import get_db

PW = "correct horse battery"


@pytest.fixture
def api(client, monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    models.Base.metadata.create_all(engine, tables=[
        models.Role.__table__, models.User.__table__,
        models.EmailVerificationToken.__table__])
    Session = sessionmaker(bind=engine)
    with Session() as s:
        s.add_all([models.Role(role="user"), models.Role(role="admin")])
        s.commit()

    def _db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    sent = []
    monkeypatch.setattr(mailer, "send", lambda to, subject, body, **kw: sent.append((to, body)) or True)
    monkeypatch.setattr(mailer, "APP_BASE_URL", "https://dataaisys.com")
    from main import app
    app.dependency_overrides[get_db] = _db
    yield client, sent, Session
    app.dependency_overrides.pop(get_db, None)


def _token(body: str) -> str:
    return re.search(r"/verify-email\?token=(\S+)", body).group(1)


def test_signup_verify_then_login(api):
    client, sent, Session = api
    r = client.post("/auth/signup", json={"name": "Ada", "email": "Ada@Example.com", "password": PW})
    assert r.status_code == 202
    assert len(sent) == 1 and sent[0][0] == "ada@example.com"

    # Pending: correct password still cannot log in.
    r = client.post("/auth/login", json={"email": "ada@example.com", "password": PW})
    assert r.status_code == 403

    assert client.post("/auth/verify-email", json={"token": "nope"}).status_code == 400
    assert client.post("/auth/verify-email", json={"token": _token(sent[0][1])}).status_code == 204

    r = client.post("/auth/login", json={"email": "ada@example.com", "password": PW})
    assert r.status_code == 200
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"}).json()
    assert me["role"] == "user"  # never admin/trader from a sign-up


def test_existing_account_gets_the_same_reply_and_no_mail(api):
    client, sent, Session = api
    with Session() as s:
        s.add(models.User(name="Op", email="op@example.com", password_hash="x"))
        s.commit()
    r = client.post("/auth/signup", json={"name": "X", "email": "op@example.com", "password": PW})
    assert r.status_code == 202
    assert sent == []


def test_signup_is_refused_trading_routes(api):
    client, sent, _ = api
    client.post("/auth/signup", json={"name": "Ada", "email": "ada@example.com", "password": PW})
    client.post("/auth/verify-email", json={"token": _token(sent[0][1])})
    tok = client.post("/auth/login", json={"email": "ada@example.com", "password": PW}).json()["access_token"]
    assert client.get("/trading/settings", headers={"Authorization": f"Bearer {tok}"}).status_code == 403


def test_weak_password_refused(api):
    client, sent, _ = api
    r = client.post("/auth/signup", json={"name": "Ada", "email": "ada@example.com", "password": "short"})
    assert r.status_code == 422
    assert sent == []
