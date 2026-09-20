"""The public inquiry endpoint (section 202).

Two properties matter more than the happy path and both are testable without
mail or a database: it is reachable WITHOUT a bearer (the whole reason it
exists -- the old demo form posted to an admin-only route), and a tripped
honeypot is answered exactly like success while doing nothing.
"""

import pytest


def test_inquiry_is_public_and_honeypot_is_silent(client, monkeypatch):
    """No Authorization header, honeypot filled: 202, no id, no mail, no row."""
    import routes.contact_router as cr

    sent = []
    monkeypatch.setattr(cr.mailer, "send_inquiry_notification",
                        lambda **kw: sent.append(("owner", kw)) or True)
    monkeypatch.setattr(cr.mailer, "send_inquiry_acknowledgement",
                        lambda *a: sent.append(("visitor", a)) or True)

    res = client.post("/api/contact/inquiry", json={
        "first_name": "Bot", "email": "bot@example.com",
        "message": "buy now", "website": "http://spam.example",
    })
    assert res.status_code == 202, res.text
    assert res.json() == {"status": "received", "id": None}
    assert sent == []


def test_inquiry_rejects_bad_input(client):
    """Validation, not a 401: a missing name or a non-address is a 422."""
    res = client.post("/api/contact/inquiry", json={"email": "not-an-address"})
    assert res.status_code == 422
    res = client.post("/api/contact/inquiry", json={
        "first_name": "", "email": "a@example.com"})
    assert res.status_code == 422


def test_inquiry_stores_and_mails(client, db_available, monkeypatch):
    """The real path: a row lands in registrations and both mails are queued,
    the owner's with the visitor's message, the visitor's without it."""
    if not db_available:
        pytest.skip("Postgres unreachable")
    import routes.contact_router as cr

    sent = []
    monkeypatch.setattr(cr.mailer, "send_inquiry_notification",
                        lambda **kw: sent.append(("owner", kw)) or True)
    monkeypatch.setattr(cr.mailer, "send_inquiry_acknowledgement",
                        lambda *a: sent.append(("visitor", a)) or True)

    res = client.post("/api/contact/inquiry", json={
        "first_name": "Ada", "last_name": "Lovelace",
        "email": "Ada@Example.com", "company": "Analytical Engines",
        "interest": "Agentic AI", "message": "Tell me about the auto-trader.",
    })
    assert res.status_code == 202, res.text
    body = res.json()
    assert body["status"] == "received" and isinstance(body["id"], int)

    kinds = [k for k, _ in sent]
    assert kinds == ["owner", "visitor"]
    owner = sent[0][1]
    assert owner["email"] == "ada@example.com"
    assert owner["message"] == "Tell me about the auto-trader."
    assert owner["registration_id"] == body["id"]
    visitor_args = sent[1][1]
    assert visitor_args[0] == "ada@example.com"
    assert visitor_args[1] == "Ada"
    assert "auto-trader" not in " ".join(map(str, visitor_args))

    # Clean up the row so the test is repeatable against a shared database.
    from config.db_pgrs import SessionLocal
    import models_pgdb.models as models
    with SessionLocal() as db:
        db.query(models.Registraion).filter(models.Registraion.id == body["id"]).delete()
        db.commit()
