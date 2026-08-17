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
from helpers.jwt_tokens import ACCESS_TOKEN, TokenError, decode_token

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
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None:
        raise unauthorized

    try:
        payload = decode_token(credentials.credentials, expected_type=ACCESS_TOKEN)
    except TokenError:
        raise unauthorized from None

    user = db.query(models.User).filter(models.User.id == int(payload["sub"])).first()

    # Re-read the user rather than trusting the token's claims: a token stays
    # validly signed until it expires, so a user disabled or deleted a minute
    # ago would still present a perfectly good one.
    if user is None or user.disabled:
        raise unauthorized
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
