"""Authentication routes: login, refresh, and whoami.

These issue tokens but enforce nothing elsewhere — applying the dependencies
in helpers/auth_deps.py to the existing routers is a separate step, so that
turning authentication *on* is a reviewable change rather than a side effect
of adding a login.
"""

import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
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
from helpers.pwd import Hasher

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
