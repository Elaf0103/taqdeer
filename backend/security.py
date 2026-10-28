"""
Password hashing utilities — shared by auth.py and the startup admin seed.

Algorithm: PBKDF2-HMAC-SHA256, 260 000 iterations, 16-byte random salt.
Storage format: base64( salt[16] || key[32] )

Stdlib-only — no passlib, bcrypt, or external crypto library required.
"""

import hashlib
import hmac
import os
from base64 import b64decode, b64encode


def hash_password(password: str) -> str:
    """Return a salted PBKDF2-HMAC-SHA256 hash encoded as base64."""
    salt = os.urandom(16)
    key  = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 260_000)
    return b64encode(salt + key).decode()


def verify_password(password: str, stored_hash: str) -> bool:
    """
    Constant-time comparison of a plain password against a stored hash.
    Returns False on any decoding error rather than raising.
    """
    try:
        data       = b64decode(stored_hash.encode())
        salt       = data[:16]
        stored_key = data[16:]
        key        = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 260_000)
        return hmac.compare_digest(key, stored_key)
    except Exception:
        return False
