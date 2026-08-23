"""Authentication routes: login, refresh, and whoami.

These issue tokens but enforce nothing elsewhere — applying the dependencies
in helpers/auth_deps.py to the existing routers is a separate step, so that
turning authentication *on* is a reviewable change rather than a side effect
of adding a login.
"""

import hashlib
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

import models_pgdb.models as models
from helpers.auth_deps import CurrentUser, get_db
from helpers.jwt_tokens import (
    ACCESS_TOKEN_EXPIRES_IN,
    REFRESH_TOKEN,
    TokenError,
    create_access_token,
    create_refresh_token,
    decode_token,
)
from helpers import mailer
from helpers.pwd import MIN_PASSWORD_LENGTH, Hasher, password_problem

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Auth"])

db_dependency = Annotated[Session, Depends(get_db)]


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in_minutes: int


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=MIN_PASSWORD_LENGTH)


class MeResponse(BaseModel):
    id: int
    name: str
    email: str
    role: Optional[str] = None


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, db: db_dependency):
    user = db.query(models.User).filter(models.User.email == body.email).first()

    # One message for every failure below. Distinguishing "no such account"
    # from "wrong password" would let anyone enumerate which emails are
    # registered, and a seeded account with no password set is not a
    # different kind of no.
    invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Incorrect email or password",
    )

    if user is None or not user.password_hash:
        raise invalid
    if user.disabled:
        raise invalid
    if not Hasher.verify_password(body.password, user.password_hash):
        raise invalid

    role = user.role.role if user.role is not None else None
    logger.info("Login succeeded for user id=%s role=%s", user.id, role)

    return TokenResponse(
        access_token=create_access_token(user.id, role),
        refresh_token=create_refresh_token(user.id, role),
        expires_in_minutes=ACCESS_TOKEN_EXPIRES_IN,
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest, db: db_dependency):
    try:
        payload = decode_token(body.refresh_token, expected_type=REFRESH_TOKEN)
    except TokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        ) from None

    user = db.query(models.User).filter(models.User.id == int(payload["sub"])).first()
    if user is None or user.disabled:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )

    # Role is re-read rather than carried over from the old token, so a
    # promotion or demotion takes effect on the next refresh.
    role = user.role.role if user.role is not None else None
    return TokenResponse(
        access_token=create_access_token(user.id, role),
        refresh_token=create_refresh_token(user.id, role),
        expires_in_minutes=ACCESS_TOKEN_EXPIRES_IN,
    )


@router.get("/me", response_model=MeResponse)
async def me(user: CurrentUser):
    """Whoami — also the simplest way to confirm a token is being accepted."""
    return MeResponse(
        id=user.id,
        name=user.name,
        email=user.email,
        role=user.role.role if user.role is not None else None,
    )


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(body: ChangePasswordRequest, db: db_dependency,
                          user: CurrentUser, background: BackgroundTasks):
    """Change your own password, proving you know the current one.

    The self-service half of recovery. It covers everything except a password
    genuinely forgotten, which needs POST /users/{id}/reset-password from an
    admin because nothing here can send an email.

    Requiring the current password is what stops a borrowed session from
    becoming a permanent takeover: an attacker with a stolen access token has
    at most its remaining lifetime, and cannot extend that into ownership of
    the account without also knowing the password.
    """
    if not user.password_hash or not Hasher.verify_password(body.current_password, user.password_hash):
        # 403 rather than 401: the caller IS authenticated, and a 401 would
        # tell a client its token had expired and send it to re-login.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Current password is incorrect")

    problem = password_problem(body.new_password)
    if problem:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=problem)
    if body.new_password == body.current_password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="New password must differ from the current one")

    user.password_hash = Hasher.get_password_hash(body.new_password)
    db.commit()

    # Sent even though the user just did this deliberately: the notification
    # is not for them, it is for the case where it was not them.
    background.add_task(mailer.send_password_changed, user.email,
                        _now().strftime("%Y-%m-%d %H:%M UTC"), False)
    # Deliberately no password material, not even its length.
    logger.info("Password changed by user id=%s", user.id)


# ---------------------------------------------------------------------------
# Forgotten passwords
# ---------------------------------------------------------------------------

RESET_TOKEN_MINUTES = int(os.getenv("PASSWORD_RESET_MINUTES", "30"))

# How many live reset tokens one account may hold. Not a rate limiter so much
# as a mailbox limiter: without it, anyone can make this endpoint send an
# unlimited number of emails to an address they do not own.
MAX_LIVE_RESETS = int(os.getenv("PASSWORD_RESET_MAX_LIVE", "3"))


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(..., min_length=MIN_PASSWORD_LENGTH)


def _hash_token(raw: str) -> str:
    """sha256, matching PasswordResetToken.token_hash. See the model for why
    this is not bcrypt."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


@router.post("/forgot-password", status_code=status.HTTP_204_NO_CONTENT)
async def forgot_password(body: ForgotPasswordRequest, request: Request,
                          background: BackgroundTasks, db: db_dependency):
    """Ask for a reset link.

    ALWAYS returns 204, whether or not the address belongs to an account.
    Anything else turns this into a way to test which emails are registered,
    from the one endpoint that has to be public. The same reasoning already
    governs the single error message on /auth/login.

    The mail goes out in a background task for the same reason: sending takes
    a second or two and not sending takes none, so replying only after the
    send would leak by timing what the status code refuses to say.
    """
    email = str(body.email).strip().lower()
    user = db.query(models.User).filter(models.User.email == email).first()

    if user is None or user.disabled:
        logger.info("Reset requested for %s — no eligible account. Reporting "
                    "success anyway.", email)
        return

    live = (
        db.query(models.PasswordResetToken)
        .filter(models.PasswordResetToken.user_id == user.id,
                models.PasswordResetToken.used_at.is_(None),
                models.PasswordResetToken.expires_at > _now())
        .count()
    )
    if live >= MAX_LIVE_RESETS:
        logger.warning("Reset requested for %s but %d links are already live. "
                       "Not sending another.", email, live)
        return

    raw = secrets.token_urlsafe(32)
    db.add(models.PasswordResetToken(
        user_id=user.id,
        token_hash=_hash_token(raw),
        expires_at=_now() + timedelta(minutes=RESET_TOKEN_MINUTES),
        requested_ip=(request.client.host if request.client else None),
    ))
    db.commit()

    background.add_task(mailer.send_password_reset, user.email, raw,
                        RESET_TOKEN_MINUTES)
    logger.info("Reset link issued for user id=%s, valid %d minutes.",
                user.id, RESET_TOKEN_MINUTES)


@router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(body: ResetPasswordRequest, background: BackgroundTasks,
                         db: db_dependency):
    """Spend a reset link on a new password.

    Unlike /forgot-password this DOES report failure, and has to: the caller
    is holding a token and needs to know whether it is expired, already spent,
    or simply wrong, and none of those answers reveal anything about an
    account they are not already holding a token for.
    """
    row = (
        db.query(models.PasswordResetToken)
        .filter(models.PasswordResetToken.token_hash == _hash_token(body.token))
        .first()
    )
    if row is None:
        raise HTTPException(status_code=400, detail="This reset link is not valid.")
    if row.used_at is not None:
        raise HTTPException(status_code=400,
                            detail="This reset link has already been used. Request a new one.")
    if row.expires_at <= _now():
        raise HTTPException(status_code=400,
                            detail=f"This reset link has expired (they last "
                                   f"{RESET_TOKEN_MINUTES} minutes). Request a new one.")

    problem = password_problem(body.new_password)
    if problem:
        raise HTTPException(status_code=400, detail=problem)

    user = db.query(models.User).filter(models.User.id == row.user_id).first()
    if user is None:
        raise HTTPException(status_code=400, detail="This reset link is not valid.")

    user.password_hash = Hasher.get_password_hash(body.new_password)
    row.used_at = _now()

    # Every other unused link for this account dies with it. Otherwise an
    # attacker who triggered a reset earlier still holds a working one after
    # the real owner has recovered the account.
    (db.query(models.PasswordResetToken)
       .filter(models.PasswordResetToken.user_id == user.id,
               models.PasswordResetToken.used_at.is_(None))
       .update({"used_at": _now()}, synchronize_session=False))
    db.commit()

    background.add_task(mailer.send_password_changed, user.email,
                        _now().strftime("%Y-%m-%d %H:%M UTC"), False)
    logger.info("Password reset completed for user id=%s.", user.id)


@router.get("/reset-password", response_class=HTMLResponse, include_in_schema=False)
async def reset_password_page(token: str = ""):
    """The page the emailed link opens.

    Exists because there is no frontend. Without it the link in the email
    would have to be "POST this string to an API endpoint", which is not a
    password reset anyone outside this repository can complete.

    Deliberately one self-contained file with no external assets: it is served
    from the same origin it posts to, and a reset page that pulls a script
    from somewhere else is a reset page someone else can rewrite.
    """
    safe = token.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")
    return HTMLResponse(_RESET_PAGE.replace("__TOKEN__", safe)
                                   .replace("__MINLEN__", str(MIN_PASSWORD_LENGTH)))


_RESET_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Choose a new password</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 16px/1.5 system-ui, -apple-system, sans-serif; max-width: 26rem;
         margin: 4rem auto; padding: 0 1.25rem; }
  h1 { font-size: 1.35rem; margin-bottom: .25rem; }
  p.sub { color: #6b7280; margin-top: 0; }
  label { display: block; margin: 1.25rem 0 .35rem; font-weight: 600; }
  input { width: 100%; padding: .6rem .7rem; font-size: 1rem;
          border: 1px solid #9ca3af; border-radius: .4rem; box-sizing: border-box; }
  button { margin-top: 1.25rem; width: 100%; padding: .7rem; font-size: 1rem;
           border: 0; border-radius: .4rem; background: #1a56c4; color: #fff;
           cursor: pointer; }
  button[disabled] { opacity: .6; cursor: default; }
  #msg { margin-top: 1rem; padding: .7rem .8rem; border-radius: .4rem; display: none; }
  #msg.err { background: #fdecea; color: #8a1c11; }
  #msg.ok  { background: #e6f4ea; color: #10632a; }
</style></head><body>
<h1>Choose a new password</h1>
<p class="sub">At least __MINLEN__ characters. The link works once.</p>
<form id="f">
  <label for="p">New password</label>
  <input id="p" type="password" autocomplete="new-password" required minlength="__MINLEN__">
  <label for="c">Confirm new password</label>
  <input id="c" type="password" autocomplete="new-password" required>
  <button type="submit">Set password</button>
</form>
<div id="msg"></div>
<script>
var token = "__TOKEN__";
var f = document.getElementById('f'), msg = document.getElementById('msg');
function show(text, ok) {
  msg.textContent = text; msg.className = ok ? 'ok' : 'err'; msg.style.display = 'block';
}
if (!token) show('This link is missing its token. Use the link from the email exactly as sent.', false);
f.addEventListener('submit', function (e) {
  e.preventDefault();
  var p = document.getElementById('p').value, c = document.getElementById('c').value;
  if (p !== c) { show('The two passwords do not match.', false); return; }
  var btn = f.querySelector('button'); btn.disabled = true;
  fetch('/auth/reset-password', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({token: token, new_password: p})
  }).then(function (r) {
    if (r.status === 204) {
      f.style.display = 'none';
      show('Password set. You can now sign in with your new password.', true);
      return;
    }
    return r.json().catch(function () { return {}; }).then(function (b) {
      show(b.detail || 'That did not work. Request a new link.', false);
      btn.disabled = false;
    });
  }).catch(function () {
    show('Could not reach the server. Try again.', false); btn.disabled = false;
  });
});
</script></body></html>"""
