"""
CRUD router — /results
Supports exam-results.html, exam-review.html, and results-reviews.html.

Endpoint map
────────────
GET  /results/                                       list all results for an exam
GET  /results/stats?exam_id=...                      aggregate stats (avg, high, low, pass rate)
POST /results/approve-all?exam_id=...                approve all pending results for an exam
POST /results/seed                                   seed realistic test data (dev / testing only)
GET  /results/{result_id}                            single student result
PUT  /results/{result_id}                            manual grade override
POST /results/{result_id}/approve                    approve a single result
POST /results/{result_id}/unapprove                  unlock an approved result (revert to pending_review)
GET  /results/{result_id}/question-scores            per-question breakdown for exam-review.html
PUT  /results/{result_id}/question-scores/{score_id} edit one question score, recomputes total

Note: static paths (/stats, /approve-all, /seed) are declared before /{result_id}
so FastAPI never confuses them with a numeric path parameter.
"""

import random
from typing import Dict, List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import get_db
from deps import get_current_user
from models import (
    Exam, ExamModelAnswer, ExamResult, ExamStatus, GradeStatus,
    StudentQuestionScore, User,
)
from permissions import require_exam_access
from schemas import (
    ExamResultRead, ExamResultUpdate,
    ResultSeedPayload, ResultStatsRead,
    StudentQuestionScoreRead, StudentQuestionScoreUpdate,
)

router = APIRouter(prefix="/results", tags=["Results"])

# Pass threshold: score >= 60 % of total_marks counts as a pass
_PASS_PCT = 60.0


# ── Grading helper ────────────────────────────────────────────────────────────

def _compute_grade(score: float, total_marks: int) -> str:
    """Convert a raw score to a letter grade (Saudi university GPA scale)."""
    if total_marks <= 0:
        return "F"
    pct = score / total_marks * 100
    if pct >= 95: return "A+"
    if pct >= 90: return "A"
    if pct >= 85: return "B+"
    if pct >= 80: return "B"
    if pct >= 75: return "C+"
    if pct >= 70: return "C"
    if pct >= 65: return "D+"
    if pct >= 60: return "D"
    return "F"


# ── DB helpers ────────────────────────────────────────────────────────────────

def _get_exam_or_404(exam_id: int, db: Session) -> Exam:
    exam = db.get(Exam, exam_id)
    if exam is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Exam {exam_id} not found.",
        )
    return exam


def _get_result_or_404(result_id: int, db: Session) -> ExamResult:
    result = db.get(ExamResult, result_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Result {result_id} not found.",
        )
    return result


def _get_qs_or_404(score_id: int, db: Session) -> StudentQuestionScore:
    qs = db.get(StudentQuestionScore, score_id)
    if qs is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Question score {score_id} not found.",
        )
    return qs


# ── Archive helper ────────────────────────────────────────────────────────────

def _maybe_auto_archive(exam_id: int, db: Session) -> bool:
    """
    Called after every approval action.

    If every ExamResult for this exam is now approved, automatically:
      - sets exam.status = graded
      - sets exam.is_archived = True

    This is the bridge between the results-approval workflow and the archive
    page: the archive page queries exams where is_archived=True, so without
    this call an approved exam would never appear there.

    Returns True if the exam was auto-archived, False otherwise.
    """
    all_results = (
        db.query(ExamResult)
        .filter(ExamResult.exam_id == exam_id)
        .all()
    )
    if not all_results:
        print(f"[ARCHIVE-STATUS] exam_id={exam_id} total_results=0 — nothing to archive")
        return False

    n_total    = len(all_results)
    n_approved = sum(1 for r in all_results if r.status == GradeStatus.approved)
    all_done   = n_approved == n_total

    print(
        f"[ARCHIVE-STATUS] exam_id={exam_id} "
        f"total_results={n_total} approved={n_approved} "
        f"all_approved={all_done}"
    )

    if not all_done:
        return False

    exam = db.get(Exam, exam_id)
    if exam is None:
        return False

    if not exam.is_archived:
        exam.status      = ExamStatus.graded
        exam.is_archived = True
        db.commit()
        print(
            f"[APPROVAL-ACTION] exam_id={exam_id} "
            f"updated_field=is_archived,status "
            f"value=True,graded — auto-archived: all {n_total} results approved"
        )

    return True


# ── GET /results/ ─────────────────────────────────────────────────────────────

@router.get("/", response_model=List[ExamResultRead], response_model_by_alias=True)
def list_results(
    exam_id:      int,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Return all student results for a specific exam, ordered by student_id.
    `exam_id` is required.
    """
    exam = _get_exam_or_404(exam_id, db)
    require_exam_access(exam, current_user)
    results = (
        db.query(ExamResult)
        .filter(ExamResult.exam_id == exam_id)
        .order_by(ExamResult.student_id)
        .all()
    )
    return [ExamResultRead.from_orm(r) for r in results]


# ── GET /results/stats ────────────────────────────────────────────────────────

@router.get("/stats", response_model=ResultStatsRead, response_model_by_alias=True)
def get_stats(
    exam_id:      int,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Return aggregate statistics for an exam's results.
    Used by the stats strip on exam-results.html and summary cards on results-reviews.html.
    """
    exam = _get_exam_or_404(exam_id, db)
    require_exam_access(exam, current_user)
    results = (
        db.query(ExamResult)
        .filter(ExamResult.exam_id == exam_id)
        .all()
    )

    if not results:
        return ResultStatsRead.model_validate({
            "total": 0, "graded": 0, "pending": 0, "approved": 0,
            "avgScore": 0.0, "highestScore": 0.0, "lowestScore": 0.0, "passRate": 0.0,
        })

    scores    = [float(r.total_score) for r in results]
    pass_min  = exam.total_marks * _PASS_PCT / 100
    approved  = sum(1 for r in results if r.status == GradeStatus.approved)
    pending   = len(results) - approved

    return ResultStatsRead.model_validate({
        "total":        len(results),
        "graded":       len(results),
        "pending":      pending,
        "approved":     approved,
        "avgScore":     round(sum(scores) / len(scores), 2),
        "highestScore": max(scores),
        "lowestScore":  min(scores),
        "passRate":     round(sum(1 for s in scores if s >= pass_min) / len(scores), 4),
    })


# ── POST /results/approve-all ─────────────────────────────────────────────────

@router.post("/approve-all")
def approve_all(
    exam_id:      int,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
) -> Dict[str, int]:
    """
    Approve all non-approved results for an exam in one shot.
    Returns the count of results that were just approved.
    """
    exam = _get_exam_or_404(exam_id, db)
    require_exam_access(exam, current_user)
    pending = (
        db.query(ExamResult)
        .filter(
            ExamResult.exam_id == exam_id,
            ExamResult.status  != GradeStatus.approved,
        )
        .all()
    )
    for r in pending:
        r.status = GradeStatus.approved
    db.commit()
    print(
        f"[APPROVAL-ACTION] exam_id={exam_id} "
        f"updated_field=ExamResult.status value=approved count={len(pending)}"
    )
    _maybe_auto_archive(exam_id, db)
    return {"approved": len(pending)}


# ── POST /results/seed ────────────────────────────────────────────────────────

# Realistic Arabic-English student names for a Saudi university context
_STUDENT_NAMES = [
    "Abdulrahman Al-Otaibi", "Mohammed Al-Ghamdi",  "Sara Al-Qahtani",
    "Fatima Al-Shehri",      "Omar Al-Zahrani",      "Noura Al-Dosari",
    "Khalid Al-Harbi",       "Reem Al-Mutairi",      "Abdullah Al-Maliki",
    "Hind Al-Rashidi",       "Faisal Al-Anazi",      "Maha Al-Bishi",
    "Turki Al-Subaie",       "Dana Al-Shahrani",     "Majed Al-Dosari",
    "Lama Al-Otaibi",        "Salman Al-Qahtani",    "Reema Al-Ghamdi",
    "Nasser Al-Harthi",      "Dalal Al-Zahrani",     "Ibrahim Al-Shehri",
    "Hessa Al-Mutairi",      "Yazeed Al-Harbi",      "Rawan Al-Maliki",
    "Saud Al-Anazi",         "Ghada Al-Rashidi",     "Bandar Al-Subaie",
    "Widad Al-Shahrani",     "Hamad Al-Bishi",       "Aisha Al-Dosari",
]


def _random_score_pct() -> float:
    """Return a score percentage (0.0–1.0) from a realistic exam distribution."""
    roll = random.random()
    if roll < 0.05:  return random.uniform(0.20, 0.45)  # 5%  — poor
    if roll < 0.20:  return random.uniform(0.45, 0.60)  # 15% — near-fail
    if roll < 0.45:  return random.uniform(0.60, 0.75)  # 25% — C range
    if roll < 0.70:  return random.uniform(0.75, 0.88)  # 25% — B range
    if roll < 0.90:  return random.uniform(0.88, 0.97)  # 20% — A range
    return           random.uniform(0.97, 1.00)          # 10% — near-perfect


def _per_question_scores(target_pct: float, questions: list) -> list[float]:
    """
    Generate per-question scores for a student whose overall target is target_pct.
    Each question independently varies ±25 % around the target, then is clamped
    to [0, max_score] and rounded to the nearest 0.5 for exam realism.
    """
    result = []
    for q in questions:
        max_q = float(q.score)
        variation = random.uniform(-0.25, 0.25)
        raw = max_q * max(0.0, min(1.0, target_pct + variation))
        rounded = round(raw * 2) / 2        # nearest 0.5
        result.append(min(max_q, max(0.0, rounded)))
    return result


@router.post("/seed", response_model=List[ExamResultRead], response_model_by_alias=True,
             status_code=status.HTTP_201_CREATED)
def seed_results(
    payload:      ResultSeedPayload,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Seed realistic test results for an exam.  **Dev / testing use only.**

    • Requires the exam to have at least one model answer (used for per-question scores).
    • Deletes any existing results for this exam before seeding (safe to call multiple times).
    • `studentCount` defaults to `exam.student_count` when omitted.
    • Generates a realistic score distribution (see _random_score_pct).
    • student_answer and ai_reasoning are left NULL — they will be filled by the
      AI grading engine in a later phase.
    """
    exam = _get_exam_or_404(payload.exam_id, db)
    require_exam_access(exam, current_user)

    questions = (
        db.query(ExamModelAnswer)
        .filter(ExamModelAnswer.exam_id == payload.exam_id)
        .order_by(ExamModelAnswer.question_number)
        .all()
    )
    if not questions:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Cannot seed results: this exam has no model answers. "
                "Add an answer key first via POST /model-answers/."
            ),
        )

    # Delete existing results — CASCADE removes StudentQuestionScore rows
    for existing in db.query(ExamResult).filter(ExamResult.exam_id == payload.exam_id).all():
        db.delete(existing)
    db.flush()

    n     = payload.student_count or exam.student_count
    names = (_STUDENT_NAMES * ((n // len(_STUDENT_NAMES)) + 1))[:n]

    created: list[ExamResult] = []
    for i, name in enumerate(names):
        pct       = _random_score_pct()
        q_scores  = _per_question_scores(pct, questions)
        total     = round(sum(q_scores), 2)

        result = ExamResult(
            exam_id         = payload.exam_id,
            student_id      = f"STU-{i + 1:03d}",
            student_name    = name,
            total_score     = total,
            grade           = _compute_grade(total, exam.total_marks),
            status          = GradeStatus.graded,
            manually_edited = False,
        )
        db.add(result)
        db.flush()      # assign result.id before creating child rows

        for q, q_score in zip(questions, q_scores):
            db.add(StudentQuestionScore(
                result_id       = result.id,
                question_number = q.question_number,
                question_text   = q.question_text,
                student_answer  = None,     # filled by AI grading engine later
                ai_reasoning    = None,     # filled by AI grading engine later
                max_score       = q.score,
                score           = q_score,
                original_score  = q_score,  # seed value = original; never mutated after
            ))

        created.append(result)

    db.commit()
    for r in created:
        db.refresh(r)

    return [ExamResultRead.from_orm(r) for r in created]


# ── GET /results/{result_id} ──────────────────────────────────────────────────

@router.get("/{result_id}", response_model=ExamResultRead, response_model_by_alias=True)
def get_result(
    result_id:    int,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """Return a single student result by ID."""
    result = _get_result_or_404(result_id, db)
    exam   = _get_exam_or_404(result.exam_id, db)
    require_exam_access(exam, current_user)
    return ExamResultRead.from_orm(result)


# ── PUT /results/{result_id} ──────────────────────────────────────────────────

@router.put("/{result_id}", response_model=ExamResultRead, response_model_by_alias=True)
def update_result(
    result_id:    int,
    payload:      ExamResultUpdate,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Manual grade override from the results-reviews.html edit modal.

    • Accepts a new totalScore (0 → exam.total_marks).
    • Recomputes the letter grade when totalScore changes.
    • Sets status = overridden and manually_edited = True on any score change.
    """
    result = _get_result_or_404(result_id, db)
    exam   = _get_exam_or_404(result.exam_id, db)
    require_exam_access(exam, current_user)

    if result.status == GradeStatus.approved:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Grade is approved and locked. Use POST /results/{id}/unapprove to unlock before editing.",
        )

    update_data = payload.model_dump(exclude_none=True, by_alias=False)

    if "total_score" in update_data:
        exam      = _get_exam_or_404(result.exam_id, db)
        new_score = float(update_data["total_score"])
        if new_score > exam.total_marks:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Score {new_score} exceeds total marks ({exam.total_marks}).",
            )
        result.total_score     = new_score
        result.grade           = _compute_grade(new_score, exam.total_marks)
        result.manually_edited = True
        result.status          = GradeStatus.overridden

    db.commit()
    db.refresh(result)
    return ExamResultRead.from_orm(result)


# ── POST /results/{result_id}/approve ────────────────────────────────────────

@router.post("/{result_id}/approve",
             response_model=ExamResultRead, response_model_by_alias=True)
def approve_result(
    result_id:    int,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """Approve a single student result (locks it from further editing)."""
    result = _get_result_or_404(result_id, db)
    exam   = _get_exam_or_404(result.exam_id, db)
    require_exam_access(exam, current_user)
    result.status = GradeStatus.approved
    db.commit()
    print(
        f"[APPROVAL-ACTION] exam_id={result.exam_id} result_id={result_id} "
        f"updated_field=ExamResult.status value=approved"
    )
    _maybe_auto_archive(result.exam_id, db)
    db.refresh(result)
    return ExamResultRead.from_orm(result)


# ── POST /results/{result_id}/unapprove ──────────────────────────────────────

@router.post("/{result_id}/unapprove",
             response_model=ExamResultRead, response_model_by_alias=True)
def unapprove_result(
    result_id:    int,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """Unlock an approved result — reverts status to pending_review so the grade can be edited again."""
    result = _get_result_or_404(result_id, db)
    exam   = _get_exam_or_404(result.exam_id, db)
    require_exam_access(exam, current_user)
    if result.status != GradeStatus.approved:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Result is not approved; nothing to unlock.",
        )
    result.status = GradeStatus.pending_review
    db.commit()
    db.refresh(result)
    return ExamResultRead.from_orm(result)


# ── GET /results/{result_id}/question-scores ──────────────────────────────────

@router.get("/{result_id}/question-scores",
            response_model=List[StudentQuestionScoreRead],
            response_model_by_alias=True)
def list_question_scores(
    result_id:    int,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Return per-question score breakdown for one student result.
    Used by exam-review.html to render the question cards.
    """
    result = _get_result_or_404(result_id, db)
    exam   = _get_exam_or_404(result.exam_id, db)
    require_exam_access(exam, current_user)
    scores = (
        db.query(StudentQuestionScore)
        .filter(StudentQuestionScore.result_id == result_id)
        .order_by(StudentQuestionScore.question_number)
        .all()
    )
    return [StudentQuestionScoreRead.from_orm(s) for s in scores]


# ── PUT /results/{result_id}/question-scores/{score_id} ──────────────────────

@router.put("/{result_id}/question-scores/{score_id}",
            response_model=StudentQuestionScoreRead,
            response_model_by_alias=True)
def update_question_score(
    result_id:    int,
    score_id:     int,
    payload:      StudentQuestionScoreUpdate,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Edit a single question score from exam-review.html.

    • Validates new score is in [0, question.max_score].
    • Recomputes ExamResult.total_score and letter grade atomically.
    • Sets ExamResult.manually_edited = True.
    • Raises 409 if result.status == approved (must unlock first via /unapprove).
    • Never mutates original_score (audit trail preserved).
    """
    qs = _get_qs_or_404(score_id, db)
    if qs.result_id != result_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Question score {score_id} does not belong to result {result_id}.",
        )
    # Permission check — resolve exam through result
    _result_for_auth = _get_result_or_404(result_id, db)
    _exam_for_auth   = _get_exam_or_404(_result_for_auth.exam_id, db)
    require_exam_access(_exam_for_auth, current_user)

    if payload.score > float(qs.max_score):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Score {payload.score} exceeds the maximum "
                f"({float(qs.max_score)}) for this question."
            ),
        )

    # Block edits on approved results — instructor must explicitly unlock first
    _result_check = _get_result_or_404(result_id, db)
    if _result_check.status == GradeStatus.approved:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Grade is approved and locked. Use POST /results/{id}/unapprove to unlock before editing.",
        )

    qs.score = payload.score
    db.flush()

    # Recompute parent result within the same transaction
    result    = _get_result_or_404(result_id, db)
    exam      = _get_exam_or_404(result.exam_id, db)
    new_total = float(
        db.query(func.sum(StudentQuestionScore.score))
        .filter(StudentQuestionScore.result_id == result_id)
        .scalar() or 0
    )

    result.total_score     = round(new_total, 2)
    result.grade           = _compute_grade(new_total, exam.total_marks)
    result.manually_edited = True

    db.commit()
    db.refresh(qs)
    return StudentQuestionScoreRead.from_orm(qs)
