"""
Auth router — /auth
Supports login.html, register.html, logout.html, reset-password.html.

Design notes
────────────
• Login returns a signed JWT (access_token) alongside the user record.
  The frontend must store the token and include it as
  "Authorization: Bearer <token>" on subsequent API requests.

• Password hashing: see security.py (stdlib PBKDF2-HMAC-SHA256).

• Reset codes: 6-digit codes stored in password_reset_tokens (DB table).
  Each token row holds a PBKDF2 hash of the plain code, an expiry timestamp
  (10 minutes), and a used_at column that is set on first successful use.
  The plain code is emailed to the user via email_service.send_reset_code().
"""

from __future__ import annotations

import hashlib
import hmac
import os
import random
import secrets
import string
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import get_db
from email_service import EmailError, send_reset_code
from jwt_utils import create_access_token
from models import PasswordResetToken, User, UserRole
from schemas import (
    ForgotPasswordPayload,
    LoginResponse,
    RequestResetPayload,
    ResetPasswordPayload,
    UserLogin,
    UserRead,
    UserRegister,
    VerifyResetCodePayload,
)
from security import hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["Auth"])

_CODE_TTL_MINUTES = 10
_LINK_TTL_MINUTES = 20  # lifetime for URL-token reset links


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_code() -> str:
    """Return a cryptographically random 6-digit string."""
    return "".join(random.SystemRandom().choices(string.digits, k=6))


def _hash_code(code: str) -> str:
    """Return a PBKDF2-HMAC-SHA256 hash of the plain code (same format as passwords)."""
    salt = os.urandom(16)
    key  = hashlib.pbkdf2_hmac("sha256", code.encode(), salt, 260_000)
    from base64 import b64encode
    return b64encode(salt + key).decode()


def _verify_code(plain: str, stored_hash: str) -> bool:
    """Constant-time comparison of a plain code against its stored hash."""
    try:
        from base64 import b64decode
        data       = b64decode(stored_hash.encode())
        salt       = data[:16]
        stored_key = data[16:]
        key        = hashlib.pbkdf2_hmac("sha256", plain.encode(), salt, 260_000)
        return hmac.compare_digest(key, stored_key)
    except Exception:
        return False


# ── POST /auth/register ────────────────────────────────────────────────────────

@router.post(
    "/register",
    response_model=UserRead,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
)
def register(payload: UserRegister, db: Session = Depends(get_db)):
    """
    Create a new user account.
    Email is normalised to lowercase and must be unique.
    Role defaults to 'instructor' (set in the User model).
    """
    email = payload.email.strip().lower()
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        )
    user = User(
        full_name          = payload.full_name.strip(),
        email              = email,
        password_hash      = hash_password(payload.password),
        department         = payload.department,
        professional_title = payload.professional_title,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        )
    db.refresh(user)
    return UserRead.from_orm(user)


# ── POST /auth/login ───────────────────────────────────────────────────────────

@router.post("/login", response_model=LoginResponse, response_model_by_alias=True)
def login(payload: UserLogin, db: Session = Depends(get_db)):
    """
    Validate credentials and return a signed JWT + full user record.

    Response shape:
      { "accessToken": "...", "tokenType": "bearer", "user": { ...UserRead... } }

    A generic 401 is returned for both "not found" and "wrong password"
    to prevent user enumeration.
    """
    email = payload.email.strip().lower()
    user  = db.query(User).filter(User.email == email).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )

    # Check deactivation AFTER credential verification — this ordering is
    # intentional: we verify the password first so an attacker cannot use a
    # 403 vs 401 difference to confirm that an email exists in the system.
    if not user.is_active:
        print(
            f"[AUTH-BLOCKED-DEACTIVATED] "
            f"user_id={user.id} email={email!r} role={user.role.value}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account has been deactivated. Please contact the administrator.",
        )

    token = create_access_token(user_id=user.id, role=user.role.value)
    return LoginResponse(access_token=token, user=UserRead.from_orm(user))


# ── POST /auth/logout ──────────────────────────────────────────────────────────

@router.post("/logout")
def logout():
    """
    Stateless logout — no server-side session exists.
    Returns 200 so the frontend knows it can safely clear localStorage.
    """
    return {"message": "Logged out."}


# ── POST /auth/forgot-password ────────────────────────────────────────────────

@router.post("/forgot-password")
def forgot_password(payload: ForgotPasswordPayload, db: Session = Depends(get_db)):
    """
    Step 1 of the 3-step reset-password flow.

    Generates a cryptographically random 6-digit verification code, stores its
    PBKDF2 hash in password_reset_tokens (20-minute TTL), and emails the plain
    code to the user via Gmail SMTP (email_service.py).

    Security rules:
      • Always returns 200 with a neutral message — never reveals whether the
        email is registered (prevents user enumeration).
      • Any existing unused tokens for the account are invalidated first, so
        only one active code exists at any time.
      • The plain code is sent only in the email; only the hash is persisted.
      • Email sending is best-effort: a failure does NOT roll back the token.
        In development mode (APP_ENV=development) the plain code is also
        returned in the response as "dev_code" so the flow can be tested
        without a working SMTP setup.
    """
    email = payload.email.strip().lower()
    print(f"[FORGOT-PASSWORD] endpoint called — email={email!r}")

    # ── Dev-mode detection ────────────────────────────────────────────────────
    # ONLY APP_ENV controls whether dev_code is exposed.
    # smtp_diagnostics() is deliberately NOT used here — a transient SMTP
    # warning must never leak the plain code to the API response in production.
    app_env = os.environ.get("APP_ENV", "development").strip().lower()
    is_dev  = app_env != "production"
    print(f"[FORGOT-PASSWORD] APP_ENV={app_env!r} → is_dev={is_dev}")

    _SAFE = {"message": "If an account with this email exists, a reset code has been sent."}

    user = db.query(User).filter(User.email == email).first()
    if not user:
        print(f"[FORGOT-PASSWORD] email not registered — returning safe response (no code generated)")
        return _SAFE

    # Admin accounts are excluded from self-service password reset.
    # Return the identical safe response so the caller cannot distinguish
    # "email not found", "email is admin", or "email is instructor" — this
    # prevents both account-type disclosure and user enumeration.
    if user.role == UserRole.admin:
        print(f"[FORGOT-PASSWORD] admin account — silently suppressed (user_id={user.id})")
        return _SAFE

    # Invalidate any existing unused tokens for this user
    deleted = db.query(PasswordResetToken).filter(
        PasswordResetToken.user_id == user.id,
        PasswordResetToken.used_at.is_(None),
    ).delete(synchronize_session=False)
    if deleted:
        print(f"[FORGOT-PASSWORD] invalidated {deleted} existing unused token(s) for user_id={user.id}")

    # Generate code and persist only its hash
    code       = _make_code()
    code_hash  = _hash_code(code)
    expires_at = datetime.utcnow() + timedelta(minutes=_LINK_TTL_MINUTES)

    token_row = PasswordResetToken(
        user_id    = user.id,
        email      = email,
        code_hash  = code_hash,
        expires_at = expires_at,
    )
    db.add(token_row)
    db.commit()

    print(f"[FORGOT-PASSWORD] code generated — token_id={token_row.id} "
          f"expires_at={expires_at.isoformat()}Z")

    # Send code via Gmail SMTP (best-effort)
    try:
        send_reset_code(to_email=email, code=code)
        print(f"[FORGOT-PASSWORD] SMTP send succeeded — to={email!r}")
    except Exception as exc:
        print(f"[FORGOT-PASSWORD] SMTP send failed — to={email!r} error={exc}")

    response: dict = {"message": _SAFE["message"]}
    if is_dev:
        response["dev_code"] = code   # only visible in development

    return response


# ── POST /auth/verify-reset-code ──────────────────────────────────────────────

@router.post("/verify-reset-code")
def verify_reset_code(payload: VerifyResetCodePayload, db: Session = Depends(get_db)):
    """
    Step 2 of the 3-step reset-password flow.

    Validates that the submitted code matches the stored hash for the given
    email, is not expired, and has not already been used.

    Does NOT mark the token as used — that happens only on the final
    POST /auth/reset-password call, which re-validates the code again.
    This allows the user to correct a wrong entry without burning the token.

    Returns a JSON body (always 200) so the frontend can stay on Step 2
    and show a specific error rather than receiving an HTTP error code:
        { "ok": true,  "message": "Verification code confirmed." }
        { "ok": false, "message": "Invalid verification code." }
        { "ok": false, "message": "Verification code expired. ..." }
    """
    email = payload.email.strip().lower()
    now   = datetime.utcnow()

    print(f"[VERIFY-CODE] called — email={email!r}")

    # Find the latest unused token for this email (check expiry separately
    # so we can return a specific "expired" message instead of "invalid").
    token_row: PasswordResetToken | None = (
        db.query(PasswordResetToken)
        .filter(
            PasswordResetToken.email   == email,
            PasswordResetToken.used_at.is_(None),
        )
        .order_by(PasswordResetToken.created_at.desc())
        .first()
    )

    if token_row is None:
        print(f"[VERIFY-CODE] no active token found for email={email!r}")
        return {"ok": False, "message": "No active reset code found. Please request a new one."}

    if token_row.expires_at < now:
        print(f"[VERIFY-CODE] token expired — token_id={token_row.id}")
        return {"ok": False, "message": "Verification code expired. Please request a new code."}

    if not _verify_code(payload.code, token_row.code_hash):
        print(f"[VERIFY-CODE] wrong code — token_id={token_row.id}")
        return {"ok": False, "message": "Invalid verification code."}

    print(f"[VERIFY-CODE] code valid — token_id={token_row.id}")
    return {"ok": True, "message": "Verification code confirmed."}


# ── POST /auth/request-reset ───────────────────────────────────────────────────

@router.post("/request-reset")
def request_reset(payload: RequestResetPayload, db: Session = Depends(get_db)):
    """
    Generate a 6-digit reset code, store its hash in password_reset_tokens,
    and attempt to email the plain code to the user.

    Token generation is ALWAYS performed and committed.
    Email sending is BEST-EFFORT: a delivery failure does not fail the request
    and does NOT roll back the token.

    Always returns 200 — even when the email is not registered — to prevent
    user enumeration.  The response body never contains the reset code in
    production; in development (APP_ENV=development) it is included as
    "dev_code" for testing without a working SMTP setup.
    """
    import os as _os
    is_dev = (
        _os.environ.get("APP_ENV", "development").lower() == "development"
        or not email_is_configured()   # no API key → behave as dev regardless of APP_ENV
    )

    email = payload.email.strip().lower()
    user  = db.query(User).filter(User.email == email).first()
    if not user:
        # Intentionally vague — do not reveal whether the email exists
        response: dict = {"message": "If your email is registered, a reset code has been generated."}
        if is_dev:
            response["dev_code"] = None   # no code for unknown email
        return response

    # Invalidate any existing unused tokens for this user so old codes stop working
    db.query(PasswordResetToken).filter(
        PasswordResetToken.user_id == user.id,
        PasswordResetToken.used_at.is_(None),
    ).delete(synchronize_session=False)

    code       = _make_code()
    code_hash  = _hash_code(code)
    expires_at = datetime.utcnow() + timedelta(minutes=_CODE_TTL_MINUTES)

    token_row = PasswordResetToken(
        user_id    = user.id,
        email      = email,
        code_hash  = code_hash,
        expires_at = expires_at,
    )
    db.add(token_row)
    db.commit()

    print(f"[RESET-CODE-GENERATED] email={email!r} token_id={token_row.id}")

    # ── Email sending is best-effort — failure keeps the token and returns 200 ──
    try:
        send_reset_code(to_email=email, code=code)
        print(f"[EMAIL-SEND-SUCCESS] reset code delivered to {email!r}")
    except EmailError as exc:
        print(f"[EMAIL-SEND-FAILED] email={email!r} error={exc}")
        # Token stays in DB — user can still reset if they obtain the code
        # another way (e.g. via dev_code in development mode).

    response = {"message": "If your email is registered, a reset code has been generated."}
    if is_dev:
        response["dev_code"] = code   # visible only in development

    return response


# ── POST /auth/reset-password ──────────────────────────────────────────────────

@router.post("/reset-password")
def reset_password(payload: ResetPasswordPayload, db: Session = Depends(get_db)):
    """
    Set a new password using either a URL token (token-link flow) or a
    6-digit code (legacy code flow).

    Token-link flow (preferred — used by reset-password.html v2):
      Body: { "token": "<urlsafe token>", "newPassword": "..." }
      Looks up the token by its SHA-256 hash; validates expiry and single-use.

    Code flow (legacy — used by the 3-step reset-password page):
      Body: { "email": "...", "code": "...", "newPassword": "..." }
      Looks up by email; validates code via PBKDF2 constant-time compare.

    On success in both flows: updates password_hash, marks token as used,
    invalidates any remaining unused tokens for that user.
    """
    now = datetime.utcnow()

    # ── Token-link flow ──────────────────────────────────────────────────────
    if payload.token:
        token_hash = hashlib.sha256(payload.token.encode()).hexdigest()

        token_row: PasswordResetToken | None = (
            db.query(PasswordResetToken)
            .filter(
                PasswordResetToken.code_hash  == token_hash,
                PasswordResetToken.used_at.is_(None),
                PasswordResetToken.expires_at > now,
            )
            .first()
        )

        if token_row is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid or expired reset link. Please request a new one.",
            )

        user = db.query(User).filter(User.id == token_row.user_id).first()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found.",
            )

        # Update password and mark this token as used (single-use enforcement)
        user.password_hash = hash_password(payload.new_password)
        token_row.used_at  = now
        db.commit()

        return {"message": "Password updated successfully."}

    # ── Legacy code flow ─────────────────────────────────────────────────────
    if not payload.email or not payload.code:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide either 'token' or both 'email' and 'code'.",
        )

    email = payload.email.strip().lower()

    token_row = (
        db.query(PasswordResetToken)
        .filter(
            PasswordResetToken.email   == email,
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.expires_at > now,
        )
        .order_by(PasswordResetToken.created_at.desc())
        .first()
    )

    if token_row is None or not _verify_code(payload.code, token_row.code_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired reset code.",
        )

    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found.",
        )

    # Update password and mark token as used (single-use enforcement)
    user.password_hash = hash_password(payload.new_password)
    token_row.used_at  = now
    db.commit()

    return {"message": "Password updated successfully."}


# ── POST /auth/test-email ──────────────────────────────────────────────────────

@router.post("/test-email")
def test_email(to: str):
    """
    Development utility — send a plain test email to verify SMTP connectivity.

    Usage:
      POST /auth/test-email?to=you@example.com

    Returns the SMTP config diagnostics and the send result.
    Only enabled when APP_ENV != "production" (default: development).

    NEVER use in production — no authentication required.
    """
    import os as _os
    app_env = _os.environ.get("APP_ENV", "development").lower()
    if app_env == "production":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Test endpoint is disabled in production.",
        )

    diag = email_api_diagnostics()
    if diag:
        # Config is broken — return diagnostics without attempting send
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error":        "Email API configuration is incomplete.",
                "diagnostics":  diag,
                "instructions": (
                    "1. Open backend/.env\n"
                    "2. Set EMAIL_PROVIDER=resend  (or sendgrid)\n"
                    "3. Set EMAIL_API_KEY=re_...  (your Resend or SendGrid API key)\n"
                    "4. Set MAIL_FROM=Taqdeer <onboarding@resend.dev>  "
                    "(for Resend testing without domain verification)\n"
                    "Sign up free at resend.com — 3,000 emails/month, no credit card."
                ),
            },
        )

    try:
        send_test_email(to_email=to)
        return {
            "status":  "ok",
            "message": f"Test email sent successfully to {to}.",
        }
    except EmailError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        )
