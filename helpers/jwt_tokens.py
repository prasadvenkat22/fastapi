"""RS256 JWT signing and verification.

Keys live in the environment as base64-encoded PEM on a single line
(JWT_PRIVATE_KEY / JWT_PUBLIC_KEY) — base64 because a raw PEM's newlines
don't survive a .env file. They are decoded once at import.

RS256 rather than HS256 is deliberate and already reflected in the existing
config: only this service holds the private key and can mint tokens, while
anything that needs to *verify* one needs the public key alone. That matters
if verification ever moves to a gateway or another service.

A JWT is signed, not encrypted — anyone holding one can base64-decode the
payload and read it. Claims here are limited to identity and role for that
reason; nothing secret belongs in them.
"""

import base64
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import JWTError, jwt

ALGORITHM = os.getenv("JWT_ALGORITHM", "RS256")

# Minutes. Names match the existing env vars.
ACCESS_TOKEN_EXPIRES_IN = int(os.getenv("ACCESS_TOKEN_EXPIRES_IN", "15"))
REFRESH_TOKEN_EXPIRES_IN = int(os.getenv("REFRESH_TOKEN_EXPIRES_IN", "60"))

ACCESS_TOKEN = "access"
REFRESH_TOKEN = "refresh"


class TokenError(Exception):
    """Raised when a token is malformed, expired, or the wrong type."""


def _decode_key(var_name: str) -> str:
    raw = os.getenv(var_name)
    if not raw:
        raise RuntimeError(f"{var_name} is not set — JWT signing/verification cannot work.")
    try:
        return base64.b64decode(raw).decode("utf-8")
    except Exception as e:  # noqa: BLE001 - surface the cause plainly at startup
        raise RuntimeError(f"{var_name} is not valid base64-encoded PEM: {e}") from e


_PRIVATE_KEY = _decode_key("JWT_PRIVATE_KEY")
_PUBLIC_KEY = _decode_key("JWT_PUBLIC_KEY")


def _create_token(subject: str, role: Optional[str], token_type: str, expires_minutes: int) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(subject),          # user id
        "role": role,                 # may be None — no role means no permissions
        "type": token_type,
        "iat": now,
        "exp": now + timedelta(minutes=expires_minutes),
    }
    return jwt.encode(payload, _PRIVATE_KEY, algorithm=ALGORITHM)


def create_access_token(user_id: int, role: Optional[str]) -> str:
    return _create_token(user_id, role, ACCESS_TOKEN, ACCESS_TOKEN_EXPIRES_IN)


def create_refresh_token(user_id: int, role: Optional[str]) -> str:
    return _create_token(user_id, role, REFRESH_TOKEN, REFRESH_TOKEN_EXPIRES_IN)


def decode_token(token: str, expected_type: str = ACCESS_TOKEN) -> dict:
    """Verify signature and expiry, and confirm the token is the type the
    caller expects.

    The type check is what stops a refresh token being presented as an access
    token: both are validly signed by this service, so signature alone can't
    tell them apart, and a refresh token lives four times longer.
    """
    try:
        payload = jwt.decode(token, _PUBLIC_KEY, algorithms=[ALGORITHM])
    except JWTError as e:
        raise TokenError(str(e)) from e

    if payload.get("type") != expected_type:
        raise TokenError(f"Expected a {expected_type} token, got {payload.get('type')!r}.")
    return payload
