"""
CRUD router — /exams
Supports the Manage Exams page (manage-exams.html).
"""

from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from database import get_db
from deps import get_current_user
from models import Exam, ExamResult, ExamStatus, GradeStatus, GradingJob, GradingJobStatus, User, UserRole
from permissions import require_exam_access
from schemas import ExamCreate, ExamRead, ExamUpdate
from activity_log import log_activity

router = APIRouter(prefix="/exams", tags=["Exams"])


# ── Helpers ────────────────────────────────────────────────────────────────────

def _get_or_404(exam_id: int, db: Session) -> Exam:
    exam = db.get(Exam, exam_id)
    if exam is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Exam {exam_id} not found.",
        )
    return exam


# ── GET /exams ─────────────────────────────────────────────────────────────────

@router.get("/", response_model=List[ExamRead], response_model_by_alias=True)
def list_exams(
    archived:     bool    = False,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Return exams ordered by date descending.
    Admins see all exams; instructors see only their own.
    Pass ?archived=true to list archived exams; omit (or false) for active exams.
    """
    q = db.query(Exam).filter(Exam.is_archived == archived)
    if current_user.role != UserRole.admin:
        q = q.filter(Exam.owner_id == current_user.id)
        print(f"[ARCHIVE-FILTER] instructor_id={current_user.id} filtering by owner_id")
    exams = q.order_by(Exam.date.desc()).all()
    print(
        f"[ARCHIVE-QUERY] archived={archived} "
        f"returned_exams_count={len(exams)} "
        f"user_id={current_user.id} role={current_user.role}"
    )
    return [ExamRead.from_orm(e) for e in exams]


# ── GET /exams/{id} ────────────────────────────────────────────────────────────

@router.get("/{exam_id}", response_model=ExamRead, response_model_by_alias=True)
def get_exam(
    exam_id:      int,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """Return a single exam by ID."""
    exam = _get_or_404(exam_id, db)
    require_exam_access(exam, current_user)
    return ExamRead.from_orm(exam)


# ── POST /exams ────────────────────────────────────────────────────────────────

@router.post("/", response_model=ExamRead, response_model_by_alias=True,
             status_code=status.HTTP_201_CREATED)
def create_exam(
    payload:      ExamCreate,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Create a new exam owned by the requesting user.
    Counters (answer_files_count, graded_count, avg_score) start at 0.
    Status defaults to 'pending' and workflow_step to 1.
    """
    exam = Exam(
        owner_id      = current_user.id,
        name          = payload.name,
        type          = payload.type,
        semester      = payload.semester,
        date          = payload.date,
        section       = payload.section,
        department    = payload.dept,
        student_count = payload.students,
        total_marks   = payload.total_marks,
        answer_files_count = 0,
        graded_count       = 0,
        avg_score          = 0.00,
    )
    db.add(exam)
    db.flush()   # get exam.id before commit
    log_activity(
        db, current_user.id, "create",
        f"Exam <strong>{payload.name}</strong> created.",
    )
    db.commit()
    db.refresh(exam)
    return ExamRead.from_orm(exam)


# ── PUT /exams/{id} ────────────────────────────────────────────────────────────

@router.put("/{exam_id}", response_model=ExamRead, response_model_by_alias=True)
def update_exam(
    exam_id:      int,
    payload:      ExamUpdate,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Partial update — only the fields present in the request body are changed.
    Grading counters and status are NOT touched here.
    """
    exam = _get_or_404(exam_id, db)
    require_exam_access(exam, current_user)

    field_map = {
        "name":         "name",
        "type":         "type",
        "semester":     "semester",
        "date":         "date",
        "section":      "section",
        "dept":         "department",
        "students":     "student_count",
        "total_marks":  "total_marks",
    }
    update_data = payload.model_dump(exclude_none=True, by_alias=False)

    for pydantic_field, column in field_map.items():
        if pydantic_field in update_data:
            setattr(exam, column, update_data[pydantic_field])

    db.commit()
    db.refresh(exam)
    return ExamRead.from_orm(exam)


# ── DELETE /exams/{id} ─────────────────────────────────────────────────────────

@router.delete("/{exam_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_exam(
    exam_id:      int,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Delete an exam and all its related data via CASCADE constraints.
    """
    exam = _get_or_404(exam_id, db)
    require_exam_access(exam, current_user)
    db.delete(exam)
    db.commit()


# ── PATCH /exams/{id}/archive ──────────────────────────────────────────────────

@router.patch("/{exam_id}/archive", response_model=ExamRead, response_model_by_alias=True)
def archive_exam(
    exam_id:      int,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Mark an exam as archived.  Only graded exams can be archived.
    """
    exam = _get_or_404(exam_id, db)
    require_exam_access(exam, current_user)
    if exam.status == "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot archive an exam that has not been graded yet.",
        )
    exam.is_archived = True
    db.commit()
    db.refresh(exam)
    return ExamRead.from_orm(exam)


# ── PATCH /exams/{id}/unarchive ────────────────────────────────────────────────

@router.patch("/{exam_id}/unarchive", response_model=ExamRead, response_model_by_alias=True)
def unarchive_exam(
    exam_id:      int,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Restore an archived exam back to active state.
    """
    exam = _get_or_404(exam_id, db)
    require_exam_access(exam, current_user)
    exam.is_archived = False
    db.commit()
    db.refresh(exam)
    return ExamRead.from_orm(exam)


# ── POST /exams/{id}/regrade ───────────────────────────────────────────────────

@router.post("/{exam_id}/regrade", response_model=ExamRead, response_model_by_alias=True)
def regrade_exam(
    exam_id:      int,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Return an archived exam to the active grading workflow.

    Updates:
      - exam.is_archived  = False
      - exam.status       = reviewing
      - ExamResult.status = pending_review  (all rows for this exam)

    Does NOT modify grading_jobs, grading_final_scores, model_grading_results,
    or student_question_scores — all historical grading data is preserved so
    the instructor can inspect previous results before rerunning grading.
    """
    exam = _get_or_404(exam_id, db)
    require_exam_access(exam, current_user)

    prev_status = exam.status
    print(
        f"[REGRADING-ACTION] exam_id={exam_id} "
        f"previous_status={prev_status} new_status=reviewing "
        f"initiated_by=user_id={current_user.id}"
    )

    exam.is_archived = False
    exam.status      = ExamStatus.reviewing

    # Cancel any stuck grading jobs (pending/running) so uploads are unblocked.
    # Jobs that stall due to a server restart or background-task crash are left
    # in a non-terminal state; marking them failed here lets the instructor
    # upload new files and start a fresh grading run.
    stuck_jobs = (
        db.query(GradingJob)
        .filter(
            GradingJob.exam_id == exam_id,
            GradingJob.status.in_([GradingJobStatus.pending, GradingJobStatus.running]),
        )
        .all()
    )
    for job in stuck_jobs:
        job.status        = GradingJobStatus.failed
        job.error_message = "Cancelled by regrade — exam was reset by the instructor."
    if stuck_jobs:
        print(
            f"[REGRADING-ACTION] exam_id={exam_id} "
            f"cancelled {len(stuck_jobs)} stuck grading job(s)"
        )

    results = (
        db.query(ExamResult)
        .filter(ExamResult.exam_id == exam_id)
        .all()
    )
    for r in results:
        r.status = GradeStatus.pending_review

    db.commit()
    db.refresh(exam)

    print(
        f"[REGRADING-ACTION] exam_id={exam_id} "
        f"unlocked_results={len(results)} — exam returned to grading workflow"
    )
    return ExamRead.from_orm(exam)
