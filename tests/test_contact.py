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


# ---------------------------------------------------------------------------
# Product updates (section 203)
# ---------------------------------------------------------------------------

def test_subscribe_is_public_and_honeypot_is_silent(client, monkeypatch):
    import routes.contact_router as cr
    sent = []
    monkeypatch.setattr(cr.mailer, "send_subscribe_confirm",
                        lambda *a: sent.append(a) or True)
    res = client.post("/api/contact/subscribe", json={
        "email": "bot@example.com", "website": "x"})
    assert res.status_code == 202 and res.json() == {"status": "check_inbox"}
    assert sent == []


def test_confirm_with_garbage_token_redirects_to_invalid(client, db_available):
    if not db_available:
        pytest.skip("Postgres unreachable")
    res = client.get("/api/contact/confirm?token=nope", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/updates?updates=invalid"


def test_subscribers_list_needs_admin(client):
    assert client.get("/api/contact/subscribers").status_code == 401


def test_subscribe_confirm_unsubscribe_round_trip(client, db_available, monkeypatch):
    """Store unconfirmed, mail one link, confirm sets the flag and tells the
    owner, a second subscribe for a confirmed address sends nothing,
    unsubscribe flips it back. Nothing goes to an unconfirmed address but
    the one confirmation mail."""
    if not db_available:
        pytest.skip("Postgres unreachable")
    import routes.contact_router as cr
    import models_pgdb.models as models
    from config.db_pgrs import SessionLocal

    confirms, notices = [], []
    monkeypatch.setattr(cr.mailer, "send_subscribe_confirm",
                        lambda to, name, c, u: confirms.append((to, c, u)) or True)
    monkeypatch.setattr(cr.mailer, "send_subscriber_confirmed",
                        lambda **kw: notices.append(kw) or True)

    email = "updates-test@example.com"
    with SessionLocal() as db:
        db.query(models.Subscriber).filter(models.Subscriber.email == email).delete()
        db.commit()
    try:
        res = client.post("/api/contact/subscribe", json={
            "email": email.upper(), "name": "Ada", "source": "test"})
        assert res.status_code == 202
        assert len(confirms) == 1 and confirms[0][0] == email
        confirm_url, unsub_url = confirms[0][1], confirms[0][2]
        token = confirm_url.split("token=")[1]

        with SessionLocal() as db:
            row = db.query(models.Subscriber).filter(models.Subscriber.email == email).one()
            assert row.confirmed_at is None and row.token_hash != token

        res = client.get(f"/api/contact/confirm?token={token}", follow_redirects=False)
        assert res.headers["location"] == "/updates?updates=confirmed"
        assert len(notices) == 1 and notices[0]["email"] == email

        # Confirmed: the form must not be able to mail this address again.
        client.post("/api/contact/subscribe", json={"email": email})
        assert len(confirms) == 1

        res = client.get(f"/api/contact/unsubscribe?token={token}", follow_redirects=False)
        assert res.headers["location"] == "/updates?updates=unsubscribed"
        with SessionLocal() as db:
            row = db.query(models.Subscriber).filter(models.Subscriber.email == email).one()
            assert row.confirmed_at is not None and row.unsubscribed_at is not None
    finally:
        with SessionLocal() as db:
            db.query(models.Subscriber).filter(models.Subscriber.email == email).delete()
            db.commit()
