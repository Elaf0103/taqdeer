"""
Shared helper for writing activity log entries.

Usage (inside any router endpoint, before db.commit()):

    from activity_log import log_activity
    log_activity(db, user_id=current_user.id, type_="create", description="…")
    db.commit()

The caller is always responsible for the db.commit().
"""

from sqlalchemy.orm import Session
from models import ActivityLog


def log_activity(db: Session, user_id: int, type_: str, description: str) -> None:
    """Append one ActivityLog row.  Does NOT commit — caller must commit."""
    db.add(ActivityLog(user_id=user_id, type=type_, description=description))
