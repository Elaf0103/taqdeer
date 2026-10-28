"""
Users router — /users
Supports profile.html and admin-profile.html.

Design notes
────────────
• Identity is resolved from the JWT via get_current_user — no user_id
  query parameter is accepted or required.
• Only full_name and department are mutable; email and role are immutable.
• Avatar images are stored server-side under uploads/avatars/ and served
  via the /uploads static mount.  The stored path (e.g. uploads/avatars/3_a1b2c3d4.jpg)
  is returned as avatarPath in UserRead so the frontend can build the full URL
  as  API_BASE + '/' + avatarPath.
"""

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from database import get_db
from deps import get_current_user
from models import User
from schemas import UserRead, UserUpdate

router = APIRouter(prefix="/users", tags=["Users"])

# Avatar storage — relative to the backend/ working directory (same convention
# as uploads/answers used by answer_files.py).
_AVATAR_DIR   = Path("uploads/avatars")
_ALLOWED_MIME = {"image/png", "image/jpeg", "image/jpg"}
_MAX_BYTES    = 2 * 1024 * 1024   # 2 MB


# ── GET /users/me ──────────────────────────────────────────────────────────────

@router.get("/me", response_model=UserRead, response_model_by_alias=True)
def get_me(current_user: User = Depends(get_current_user)):
    """Return the profile of the authenticated user."""
    return UserRead.from_orm(current_user)


# ── PUT /users/me ──────────────────────────────────────────────────────────────

@router.put("/me", response_model=UserRead, response_model_by_alias=True)
def update_me(
    payload:      UserUpdate,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """Update full_name, department, and/or professional_title for the authenticated user."""
    if payload.full_name is not None:
        current_user.full_name = payload.full_name.strip()
    if payload.department is not None:
        current_user.department = payload.department
    if payload.professional_title is not None:
        current_user.professional_title = payload.professional_title
    db.commit()
    db.refresh(current_user)
    return UserRead.from_orm(current_user)


# ── POST /users/me/avatar ──────────────────────────────────────────────────────

@router.post("/me/avatar", response_model=UserRead, response_model_by_alias=True)
async def upload_my_avatar(
    file:         UploadFile = File(...),
    current_user: User       = Depends(get_current_user),
    db:           Session    = Depends(get_db),
):
    """
    Upload (or replace) the avatar image for the currently authenticated user.

    • Accepts PNG, JPG, JPEG up to 2 MB.
    • Stores the file at  uploads/avatars/{user_id}_{random8}.{ext}
    • Deletes the previous avatar file (if any) to avoid orphaned files.
    • Saves the relative path in users.avatar_path.
    • Returns the full updated UserRead so the frontend can immediately
      update taqdeer_profile.avatarPath in localStorage.

    The frontend constructs the display URL as:
        API_BASE + '/' + data.avatarPath
    e.g.  http://localhost:8000/uploads/avatars/3_a1b2c3d4.jpg
    """
    # ── Validate MIME type ─────────────────────────────────────────────────────
    if file.content_type not in _ALLOWED_MIME:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file type. Only PNG, JPG, and JPEG are accepted.",
        )

    # ── Read and size-check ────────────────────────────────────────────────────
    data = await file.read()
    if len(data) > _MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File too large. Maximum allowed size is 2 MB.",
        )

    # ── Ensure upload directory exists ────────────────────────────────────────
    _AVATAR_DIR.mkdir(parents=True, exist_ok=True)

    # ── Build unique filename — {user_id}_{8-char uuid}.{ext} ─────────────────
    original_name = file.filename or "upload"
    ext = Path(original_name).suffix.lower()
    if ext not in {".png", ".jpg", ".jpeg"}:
        ext = ".jpg"
    filename = f"{current_user.id}_{uuid.uuid4().hex[:8]}{ext}"
    dest = _AVATAR_DIR / filename

    # ── Delete old avatar to avoid orphaned files ─────────────────────────────
    if current_user.avatar_path:
        old = Path(current_user.avatar_path)
        if old.exists():
            old.unlink(missing_ok=True)

    # ── Write new file ────────────────────────────────────────────────────────
    dest.write_bytes(data)

    # ── Persist path in DB ────────────────────────────────────────────────────
    current_user.avatar_path = str(dest)   # e.g. "uploads/avatars/3_a1b2c3.jpg"
    db.commit()
    db.refresh(current_user)
    return UserRead.from_orm(current_user)
