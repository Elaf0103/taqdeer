"""
Notifications router — /notifications
Supports dashboard.html notification panel.
"""

from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from database import get_db
from deps import get_current_user
from models import Notification, User
from schemas import NotificationRead

router = APIRouter(prefix="/notifications", tags=["Notifications"])


# ── GET /notifications/ ───────────────────────────────────────────────────────

@router.get("/", response_model=List[NotificationRead], response_model_by_alias=True)
def list_notifications(
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """Return all notifications for the authenticated user, newest first."""
    entries = (
        db.query(Notification)
        .filter(Notification.user_id == current_user.id)
        .order_by(Notification.created_at.desc())
        .all()
    )
    return [NotificationRead.from_orm(e) for e in entries]


# ── PATCH /notifications/{id}/read ────────────────────────────────────────────

@router.patch(
    "/{notif_id}/read",
    response_model=NotificationRead,
    response_model_by_alias=True,
)
def mark_read(
    notif_id:     int,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """Mark a single notification as read. Must belong to the current user."""
    notif = db.get(Notification, notif_id)
    if notif is None or notif.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Notification not found.",
        )
    notif.is_read = True
    db.commit()
    db.refresh(notif)
    return NotificationRead.from_orm(notif)
