"""
Shared permission helpers used by all routers.

Access-control rules
────────────────────
• role == admin      → always allowed
• role == instructor → allowed only if exam.owner_id == caller.id
• exam.owner_id NULL → admin-only (unowned legacy/seeded exams)

Caller identity is resolved from the JWT by get_current_user (deps.py)
before these helpers are invoked.
"""

from fastapi import HTTPException, status

from models import Exam, User, UserRole


def require_exam_access(exam: Exam, caller: User) -> None:
    """
    Raise 403 if the caller is not permitted to operate on this exam.

    Admin      → always passes.
    Instructor → passes only if exam.owner_id matches caller.id.
    NULL owner → admin-only; instructors are denied.
    """
    if caller.role == UserRole.admin:
        return
    if exam.owner_id is None or exam.owner_id != caller.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to access this exam.",
        )
