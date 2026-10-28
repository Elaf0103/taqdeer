"""
CRUD routers — /rubrics  and  /model-answers
Supports the Rubrics & Answer Key page (rubrics.html).
"""

from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from database import get_db
from deps import get_current_user
from models import Exam, GradingJob, GradingJobStatus, Rubric, ExamModelAnswer, User, UserRole
from permissions import require_exam_access
from schemas import (
    RubricCreate, RubricRead, RubricUpdate,
    ModelAnswerCreate, ModelAnswerRead, ModelAnswerUpdate,
)
from activity_log import log_activity

# Two separate prefix groups combined into a single module-level router
# so main.py only needs one include_router call.
rubrics_router = APIRouter(prefix="/rubrics", tags=["Rubrics"])
answers_router = APIRouter(prefix="/model-answers", tags=["Model Answers"])


# ── Shared helpers ──────────────────────────────────────────────────────────────

def _get_exam_or_404(exam_id: int, db: Session) -> Exam:
    exam = db.get(Exam, exam_id)
    if exam is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Exam {exam_id} not found.",
        )
    return exam


def _get_rubric_or_404(rubric_id: int, db: Session) -> Rubric:
    rubric = db.get(Rubric, rubric_id)
    if rubric is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Rubric {rubric_id} not found.",
        )
    return rubric


def _get_answer_or_404(answer_id: int, db: Session) -> ExamModelAnswer:
    answer = db.get(ExamModelAnswer, answer_id)
    if answer is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Model answer {answer_id} not found.",
        )
    return answer


# ══════════════════════════════════════════════════════════════════════════════
#  /rubrics
# ══════════════════════════════════════════════════════════════════════════════

@rubrics_router.get("/", response_model=List[RubricRead], response_model_by_alias=True)
def list_rubrics(
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
    exam_id:      int     = None,
):
    """
    Return rubrics filtered by exam_id.
    Admins may omit exam_id to list all rubrics.
    Instructors may omit exam_id to list rubrics for all their own exams.
    """
    if exam_id is not None:
        exam = _get_exam_or_404(exam_id, db)
        require_exam_access(exam, current_user)
        q = db.query(Rubric).filter(Rubric.exam_id == exam_id)
    elif current_user.role == UserRole.admin:
        q = db.query(Rubric)
    else:
        owned_ids = (
            db.query(Exam.id).filter(Exam.owner_id == current_user.id).subquery()
        )
        q = db.query(Rubric).filter(Rubric.exam_id.in_(owned_ids))
    return [RubricRead.from_orm(r) for r in q.order_by(Rubric.uploaded_at.desc()).all()]


@rubrics_router.get("/{rubric_id}", response_model=RubricRead, response_model_by_alias=True)
def get_rubric(
    rubric_id:    int,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    rubric = _get_rubric_or_404(rubric_id, db)
    exam   = _get_exam_or_404(rubric.exam_id, db)
    require_exam_access(exam, current_user)
    return RubricRead.from_orm(rubric)


@rubrics_router.post("/", response_model=RubricRead, response_model_by_alias=True,
                     status_code=status.HTTP_200_OK)
def create_rubric(
    payload:      RubricCreate,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Upsert — one rubric per exam (business rule).
    If a rubric already exists for the exam, replace its file metadata in place
    (same record ID) rather than creating a duplicate row.
    """
    exam = _get_exam_or_404(payload.exam_id, db)
    require_exam_access(exam, current_user)

    existing = db.query(Rubric).filter(Rubric.exam_id == payload.exam_id).first()
    if existing:
        existing.file_name   = payload.file_name
        existing.file_size   = payload.file_size
        existing.file_path   = payload.file_path
        existing.uploaded_at = datetime.now(timezone.utc)
        if exam.workflow_step < 2:
            exam.workflow_step = 2
        log_activity(
            db, current_user.id, "upload",
            f"Rubric updated for <strong>{exam.name}</strong>.",
        )
        db.commit()
        db.refresh(existing)
        return RubricRead.from_orm(existing)

    rubric = Rubric(
        exam_id   = payload.exam_id,
        file_name = payload.file_name,
        file_size = payload.file_size,
        file_path = payload.file_path,
    )
    db.add(rubric)
    if exam.workflow_step < 2:
        exam.workflow_step = 2
    log_activity(
        db, current_user.id, "upload",
        f"Rubric uploaded for <strong>{exam.name}</strong>.",
    )
    db.commit()
    db.refresh(rubric)
    return RubricRead.from_orm(rubric)


@rubrics_router.put("/{rubric_id}", response_model=RubricRead, response_model_by_alias=True)
def update_rubric(
    rubric_id:    int,
    payload:      RubricUpdate,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """Partial update — only fields present in the request body are changed."""
    rubric = _get_rubric_or_404(rubric_id, db)
    exam   = _get_exam_or_404(rubric.exam_id, db)
    require_exam_access(exam, current_user)

    field_map = {
        "file_name": "file_name",
        "file_size": "file_size",
        "file_path": "file_path",
    }
    update_data = payload.model_dump(exclude_none=True, by_alias=False)
    for pydantic_field, column in field_map.items():
        if pydantic_field in update_data:
            setattr(rubric, column, update_data[pydantic_field])

    db.commit()
    db.refresh(rubric)
    return RubricRead.from_orm(rubric)


@rubrics_router.delete("/{rubric_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_rubric(
    rubric_id:    int,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    rubric = _get_rubric_or_404(rubric_id, db)
    exam   = _get_exam_or_404(rubric.exam_id, db)
    require_exam_access(exam, current_user)

    # Block only while a grading job is actively running.
    active_job = db.query(GradingJob).filter(
        GradingJob.exam_id == rubric.exam_id,
        GradingJob.status.in_([GradingJobStatus.pending, GradingJobStatus.running]),
    ).first()
    if active_job:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Rubric cannot be deleted while grading is in progress. Please wait for it to complete.",
        )

    db.delete(rubric)
    db.commit()


# ══════════════════════════════════════════════════════════════════════════════
#  /model-answers
# ══════════════════════════════════════════════════════════════════════════════

@answers_router.get("/", response_model=List[ModelAnswerRead], response_model_by_alias=True)
def list_model_answers(
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
    exam_id:      int     = None,
):
    """
    Return model answers ordered by question_number.
    Admins may omit exam_id; instructors must supply it.
    """
    if exam_id is not None:
        exam = _get_exam_or_404(exam_id, db)
        require_exam_access(exam, current_user)
    elif current_user.role != UserRole.admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="exam_id is required.",
        )
    q = db.query(ExamModelAnswer)
    if exam_id is not None:
        q = q.filter(ExamModelAnswer.exam_id == exam_id)
    return [ModelAnswerRead.from_orm(a) for a in q.order_by(ExamModelAnswer.question_number).all()]


@answers_router.get("/{answer_id}", response_model=ModelAnswerRead, response_model_by_alias=True)
def get_model_answer(
    answer_id:    int,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    answer = _get_answer_or_404(answer_id, db)
    exam   = _get_exam_or_404(answer.exam_id, db)
    require_exam_access(exam, current_user)
    return ModelAnswerRead.from_orm(answer)


@answers_router.post("/", response_model=ModelAnswerRead, response_model_by_alias=True,
                     status_code=status.HTTP_201_CREATED)
def create_model_answer(
    payload:      ModelAnswerCreate,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """Create a model answer entry.  Validates that the referenced exam exists."""
    exam = _get_exam_or_404(payload.exam_id, db)
    require_exam_access(exam, current_user)
    answer = ExamModelAnswer(
        exam_id         = payload.exam_id,
        question_number = payload.question_number,
        question_text   = payload.question_text,
        model_answer    = payload.model_answer,
        score           = payload.score,
        answer_type     = payload.answer_type,
    )
    db.add(answer)
    if exam.workflow_step < 3:
        exam.workflow_step = 3
    db.commit()
    db.refresh(answer)
    return ModelAnswerRead.from_orm(answer)


@answers_router.put("/{answer_id}", response_model=ModelAnswerRead, response_model_by_alias=True)
def update_model_answer(
    answer_id:    int,
    payload:      ModelAnswerUpdate,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """Partial update — only fields present in the request body are changed."""
    answer = _get_answer_or_404(answer_id, db)
    exam   = _get_exam_or_404(answer.exam_id, db)
    require_exam_access(exam, current_user)

    field_map = {
        "question_number": "question_number",
        "question_text":   "question_text",
        "model_answer":    "model_answer",
        "score":           "score",
        "answer_type":     "answer_type",
    }
    update_data = payload.model_dump(exclude_none=True, by_alias=False)
    for pydantic_field, column in field_map.items():
        if pydantic_field in update_data:
            setattr(answer, column, update_data[pydantic_field])

    if exam.workflow_step < 3:
        exam.workflow_step = 3
    db.commit()
    db.refresh(answer)
    return ModelAnswerRead.from_orm(answer)


@answers_router.delete("/{answer_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_model_answer(
    answer_id:    int,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    answer = _get_answer_or_404(answer_id, db)
    exam   = _get_exam_or_404(answer.exam_id, db)
    require_exam_access(exam, current_user)
    db.delete(answer)
    db.commit()


# ── Combined router exported to main.py ────────────────────────────────────────

router = APIRouter()
router.include_router(rubrics_router)
router.include_router(answers_router)
