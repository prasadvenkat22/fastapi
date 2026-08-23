"""FastAPI dependencies for authentication and role checks.

Defined here but deliberately not yet applied to any route — wiring them in
changes who can call what, which is a separate, reviewable step.

Role model:
    admin   full access, including the trading engine
    trader  trading routes only
    user    read access to business records

A NULL role_id means no role assigned, which reads as *no permissions*
rather than a default grant. Every account created before roles existed has
one, so the opposite reading would have silently promoted them.
"""

from typing import Annotated, Iterable

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

import models_pgdb.models as models
from config.db_pgrs import SessionLocal
from helpers.jwt_tokens import (
    ACCESS_TOKEN,
    ACCESS_TOKEN_EXPIRES_IN,
    TokenError,
    decode_token,
)

ROLE_ADMIN = "admin"
ROLE_TRADER = "trader"
ROLE_USER = "user"

# auto_error=False so a missing header produces our own 401 with a useful
# message rather than FastAPI's bare 403.
_bearer = HTTPBearer(auto_error=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: Annotated[Session, Depends(get_db)],
) -> models.User:
    # Each failure says which failure it was. These are diagnostics about a
    # token the caller is already holding, not a way to learn anything about
    # another account, so being specific gives up nothing -- and the previous
    # single "Not authenticated" for all five made a wrong header
    # indistinguishable from an expired token from a refresh token used in
    # the wrong place.
    def _401(detail: str) -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"},
        )

    if credentials is None:
        raise _401(
            "No bearer token. Send the access_token from POST /auth/login as "
            "'Authorization: Bearer <token>'. In Swagger /docs, click Authorize "
            "and paste the token alone, without the word Bearer."
        )

    try:
        payload = decode_token(credentials.credentials, expected_type=ACCESS_TOKEN)
    except TokenError as e:
        if e.code == "expired":
            raise _401(
                f"Access token has expired (they last {ACCESS_TOKEN_EXPIRES_IN} "
                "minutes). POST /auth/refresh with your refresh_token for a new one."
            ) from None
        if e.code == "wrong_type":
            raise _401(
                "That is a refresh token, not an access token. POST /auth/login "
                "returns both -- use access_token here; refresh_token only "
                "buys a new one at /auth/refresh."
            ) from None
        # The shape of what arrived, which separates a copy/paste problem from
        # a server one without echoing any of the token back. A JWT is three
        # dot-separated segments; anything else never reached the signature
        # check at all, and "signature verification failed" on a well-formed
        # three-segment token means the key changed, not the clipboard.
        raw = credentials.credentials
        segments = raw.count(".") + 1
        shape = f"{len(raw)} chars, {segments} of 3 segments"
        if raw.startswith("Bearer ") or raw.startswith("bearer "):
            hint = ("the value starts with 'Bearer' — paste the token ALONE; "
                    "Swagger and curl add that word themselves")
        elif raw[:1] in ('"', "'") or raw[-1:] in ('"', "'"):
            hint = "the value is wrapped in quotes — copy the token without them"
        elif segments != 3:
            hint = ("truncated on copy — a JWT is three dot-separated segments "
                    "and around 800 characters; select the whole value")
        else:
            hint = f"well-formed but rejected: {e}"
        raise _401(f"Token could not be verified ({shape}): {hint}.") from None

    user = db.query(models.User).filter(models.User.id == int(payload["sub"])).first()

    # Re-read the user rather than trusting the token's claims: a token stays
    # validly signed until it expires, so a user disabled or deleted a minute
    # ago would still present a perfectly good one.
    if user is None:
        raise _401("The account this token was issued for no longer exists.")
    if user.disabled:
        raise _401("This account is disabled.")
    return user


CurrentUser = Annotated[models.User, Depends(get_current_user)]


def require_role(*allowed: str):
    """Dependency factory: allow only the listed roles.

    Role is read from the database via get_current_user, not from the token
    claim, so a demotion takes effect immediately instead of lingering for
    the life of an already-issued token.
    """
    allowed_set = set(allowed)

    def _check(user: CurrentUser) -> models.User:
        role = user.role.role if user.role is not None else None
        if role not in allowed_set:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires one of: {', '.join(sorted(allowed_set))}",
            )
        return user

    return _check


def require_admin():
    return require_role(ROLE_ADMIN)


def require_trading():
    """Trading routes: admin covers trading, trader is trading-only."""
    return require_role(ROLE_ADMIN, ROLE_TRADER)
