"""
Admin-only router — /admin
Supports admin-dashborad.html: instructor listing and activate/deactivate.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import get_db
from deps import get_current_user
from models import User, UserRole
from schemas import UserAdminRead, UserRegister
from security import hash_password

router = APIRouter(prefix="/admin", tags=["Admin"])


def _require_admin(current_user: User) -> None:
    if current_user.role != UserRole.admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required.",
        )


# ── GET /admin/users ──────────────────────────────────────────────────────────

@router.get("/users", response_model=List[UserAdminRead], response_model_by_alias=True)
def list_users(
    role:         Optional[str] = None,
    current_user: User          = Depends(get_current_user),
    db:           Session       = Depends(get_db),
):
    """
    Return all users (or filtered by role).
    Admin only.  Excludes the requesting admin from the results.
    """
    _require_admin(current_user)
    q = db.query(User).filter(User.id != current_user.id)
    if role:
        try:
            role_enum = UserRole(role)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Unknown role: {role}")
        q = q.filter(User.role == role_enum)
    users = q.order_by(User.created_at.desc()).all()
    return [UserAdminRead.from_orm(u) for u in users]


# ── PATCH /admin/users/{user_id}/active ───────────────────────────────────────

@router.patch(
    "/users/{user_id}/active",
    response_model=UserAdminRead,
    response_model_by_alias=True,
)
def set_user_active(
    user_id:      int,
    active:       bool,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Set is_active for a user account.  Admin only.
    Passing ?active=true activates; ?active=false deactivates.
    An admin cannot deactivate their own account.
    """
    _require_admin(current_user)

    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail=f"User {user_id} not found.")
    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot change your own active status.")

    user.is_active = active
    db.commit()
    db.refresh(user)
    return UserAdminRead.from_orm(user)


# ── POST /admin/create-instructor ─────────────────────────────────────────────

@router.post(
    "/create-instructor",
    response_model=UserAdminRead,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
)
def create_instructor(
    payload:      UserRegister,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Admin-only: create a new instructor account.

    Differs from the public /auth/register endpoint in that:
      • Role is explicitly set to UserRole.instructor (not left to model default).
      • is_active is explicitly set to True.
      • Requires a valid admin JWT — cannot be called anonymously.
      • Returns UserAdminRead (includes is_active) instead of UserRead.

    Duplicate-email check is done at the application level first, with an
    IntegrityError fallback for race conditions on the DB unique constraint.
    """
    _require_admin(current_user)

    email = payload.email.strip().lower()
    print(
        f"[ADMIN-CREATE-INSTRUCTOR] admin_id={current_user.id} "
        f"email={email!r} role=instructor is_active=True"
    )

    # Application-level duplicate check (fast path)
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
        role               = UserRole.instructor,
        is_active          = True,
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
    print(
        f"[ADMIN-CREATE-INSTRUCTOR] success — user_id={user.id} "
        f"email={user.email!r} password_hash_created=True"
    )
    return UserAdminRead.from_orm(user)
