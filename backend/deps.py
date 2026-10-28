"""
FastAPI dependencies — shared request-scoped objects.

get_current_user
────────────────
Reads the Authorization: Bearer <token> header, decodes the JWT,
loads the corresponding User row from the database, and returns it.

Raises HTTP 401 on any of:
  • missing or malformed Authorization header
  • invalid, expired, or tampered JWT
  • user id in token does not exist in the database
"""

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from sqlalchemy.orm import Session

from database import get_db
from jwt_utils import decode_access_token
from models import User

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

_CREDENTIALS_ERROR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials.",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_current_user(
    token: str    = Depends(oauth2_scheme),
    db:   Session = Depends(get_db),
) -> User:
    """
    Resolve a Bearer token to a live User ORM object.

    Flow:
      1. FastAPI extracts the token from the Authorization header via
         OAuth2PasswordBearer (raises 401 automatically if header is absent).
      2. decode_access_token() verifies the signature and expiry.
      3. The 'sub' claim is cast to int and used to load the User row.
      4. The loaded User is returned; the same db session is shared with
         the calling endpoint via FastAPI's dependency-injection cache.
    """
    try:
        payload = decode_access_token(token)
        user_id = int(payload["sub"])
    except (JWTError, KeyError, ValueError):
        raise _CREDENTIALS_ERROR

    user = db.get(User, user_id)
    if user is None:
        raise _CREDENTIALS_ERROR

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is deactivated. Contact your administrator.",
        )

    return user
