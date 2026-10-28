"""
JWT utilities — token creation and verification.

Configuration
─────────────
Set the JWT_SECRET_KEY environment variable before deploying to production.
The fallback value is intentionally weak and must not be used in production.

Token payload
─────────────
  sub   str   user id  (stored as string per JWT spec)
  role  str   "admin" | "instructor"
  exp   int   expiry timestamp (UTC)
"""

import os
from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt  # noqa: F401  (JWTError re-exported for callers)

SECRET_KEY = os.getenv(
    "JWT_SECRET_KEY",
    "taqdeer-dev-secret-CHANGE-before-production",   # override via env var
)
ALGORITHM              = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "24"))


def create_access_token(user_id: int, role: str) -> str:
    """
    Encode a signed JWT containing user_id (as 'sub') and role.
    Tokens expire after ACCESS_TOKEN_EXPIRE_HOURS hours.
    """
    expire  = datetime.now(timezone.utc) + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    payload = {
        "sub":  str(user_id),
        "role": role,
        "exp":  expire,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict:
    """
    Decode and verify a JWT.
    Raises jose.JWTError on invalid signature, expiry, or malformed input.
    Returns the full payload dict on success.
    """
    return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
