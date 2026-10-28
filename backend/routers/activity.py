"""
Activity log router — /activity
Supports dashboard.html recent activity feed.
"""

from typing import List

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from database import get_db
from deps import get_current_user
from models import ActivityLog, User
from schemas import ActivityLogRead

router = APIRouter(prefix="/activity", tags=["Activity"])


# ── GET /activity/ ────────────────────────────────────────────────────────────

@router.get("/", response_model=List[ActivityLogRead], response_model_by_alias=True)
def list_activity(
    limit:        int     = 20,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """Return the most recent activity entries for the authenticated user."""
    entries = (
        db.query(ActivityLog)
        .filter(ActivityLog.user_id == current_user.id)
        .order_by(ActivityLog.created_at.desc())
        .limit(limit)
        .all()
    )
    return [ActivityLogRead.from_orm(e) for e in entries]


# ── DELETE /activity/ ─────────────────────────────────────────────────────────

@router.delete("/", status_code=status.HTTP_204_NO_CONTENT)
def clear_activity(
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """Delete all activity log entries for the authenticated user."""
    db.query(ActivityLog).filter(ActivityLog.user_id == current_user.id).delete()
    db.commit()
