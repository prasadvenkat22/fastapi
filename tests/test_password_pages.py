"""Password recovery meets the site (2026-09-21).

The Next.js app gained /forgot-password, /reset-password and /account pages
and a Reset password button on /users. Two things on this side had to move for
those to work, and both are cheap to pin down without mail or a database:

  * the reset LINK lands on the site's /reset-password page, not the API's
    bare fallback at /auth/reset-password (which still exists);
  * the admin users router is under /api/users, because nginx sends /api/* to
    this service and everything unlisted to the Next.js app, where /users is
    the Users PAGE. At /users the button's POST would have reached Next.
"""

import re

import pytest


def test_admin_users_router_is_under_api(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/users" in paths
    assert "/api/users/{user_id}/reset-password" in paths
    # The old prefix is gone: on the site it would be the Next.js page.
    assert not [p for p in paths if p == "/users" or p.startswith("/users/")]


def test_reset_link_lands_on_the_site_page(monkeypatch):
    from helpers import mailer

    captured = {}
    monkeypatch.setattr(mailer, "send",
                        lambda to, subject, body, **kw: captured.update(to=to, body=body) or True)
    monkeypatch.setattr(mailer, "APP_BASE_URL", "https://dataaisys.com")
    monkeypatch.setattr(mailer, "PASSWORD_RESET_PAGE", "/reset-password")

    assert mailer.send_password_reset("ada@example.com", "tok-123", 30)
    assert captured["to"] == "ada@example.com"
    links = re.findall(r"https://\S+", captured["body"])
    assert links == ["https://dataaisys.com/reset-password?token=tok-123"]
    assert "/auth/reset-password" not in captured["body"]


def test_reset_page_is_configurable_for_a_frontendless_deploy(monkeypatch):
    from helpers import mailer

    captured = {}
    monkeypatch.setattr(mailer, "send",
                        lambda to, subject, body, **kw: captured.update(body=body) or True)
    monkeypatch.setattr(mailer, "APP_BASE_URL", "http://10.0.0.5:8000")
    monkeypatch.setattr(mailer, "PASSWORD_RESET_PAGE", "/auth/reset-password")

    mailer.send_password_reset("ada@example.com", "tok", 30)
    assert "http://10.0.0.5:8000/auth/reset-password?token=tok" in captured["body"]


def test_account_created_mail_points_at_the_forgot_page(monkeypatch):
    from helpers import mailer

    captured = {}
    monkeypatch.setattr(mailer, "send",
                        lambda to, subject, body, **kw: captured.update(body=body) or True)
    monkeypatch.setattr(mailer, "APP_BASE_URL", "https://dataaisys.com")

    mailer.send_account_created("ada@example.com", "trader", has_password=False)
    assert "https://dataaisys.com/forgot-password" in captured["body"]
    assert "/auth/forgot-password" not in captured["body"]


def test_api_fallback_reset_page_still_serves(client):
    """Kept for a deployment with no frontend; PASSWORD_RESET_PAGE can point here."""
    res = client.get("/auth/reset-password?token=abc")
    assert res.status_code == 200
    assert "Choose a new password" in res.text


@pytest.mark.parametrize("path", ["/api/users", "/api/users/1/reset-password"])
def test_admin_users_routes_need_a_token(client, path):
    method = client.get if path == "/api/users" else client.post
    res = method(path)
    assert res.status_code == 401
    assert "bearer" in res.json()["detail"].lower()
