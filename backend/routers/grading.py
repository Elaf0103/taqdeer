"""
Grading Router — /grading
=========================
HTTP endpoints for the auto-grading pipeline.

Endpoints
---------
POST /grading/start           — start a grading job for an exam
GET  /grading/status/{job_id} — poll a running job's status and progress
GET  /grading/jobs/{exam_id}  — list all grading jobs for an exam
GET  /grading/results/{exam_id}/detail
                               — per-question, per-model raw grading results
                                 (for transparency reports)

The heavy lifting is delegated to services/grading_engine.py which runs
entirely in a FastAPI BackgroundTask (same pattern as extraction).

The UI polls /grading/status/{job_id} until status == "completed" or
"failed", then loads results from the existing GET /results/ endpoints.
"""

from __future__ import annotations

import json
from typing import List, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_db
from deps import get_current_user
from models import (
    Exam, ExamModelAnswer, GradingFinalScore, GradingJob,
    GradingJobStatus, ModelGradingResult, StudentExtraction, User,
)
from permissions import require_exam_access
from services.export_service import generate_csv, generate_excel, generate_pdf
from services.grading_engine import (
    apply_job_results,
    check_llm_health_for_exam,
    decline_job_results,
    finalize_job_results,
    run_grading_background,
)
from services.llm_clients import assert_keys_configured, configured_models

router = APIRouter(prefix="/grading", tags=["Grading"])


# ══════════════════════════════════════════════════════════════════════════════
#  Request / Response schemas
# ══════════════════════════════════════════════════════════════════════════════

class GradingStartRequest(BaseModel):
    exam_id:    int
    selected_models: List[str] = Field(
        ...,
        description=(
            "List of model identifiers to use. "
            "Accepted values: 'claude', 'qwen', 'deepseek', 'openai'. "
            "Pass any value (or 'all') with consensus_enabled=true to run "
            "Collaborative Consensus mode — consensus always uses Claude + OpenAI."
        ),
        min_length=1,
    )
    grading_basis: Literal["rubric", "answer_key", "both"] = "answer_key"
    consensus_enabled: bool = False
    general_grading_instructions: Optional[str] = Field(
        None,
        description="Page-level grading instructions applied to every question.",
    )
    student_ids: List[int] = Field(
        default_factory=list,
        description=(
            "Optional list of StudentExtraction IDs to grade. "
            "Empty list (default) means grade all students. "
            "When provided, only the listed students are (re)graded; "
            "existing results for other students are preserved."
        ),
    )


class GradingJobResponse(BaseModel):
    job_id:   int
    exam_id:  int
    status:   str
    progress: int
    message:  str


class GradingStatusResponse(BaseModel):
    job_id:       int
    exam_id:      int
    status:       str
    progress:     int
    started_at:   Optional[str]
    completed_at: Optional[str]
    error_message: Optional[str]
    selected_models:  Optional[list]
    grading_basis:    Optional[str]
    consensus_enabled: Optional[bool]


class ModelResultItem(BaseModel):
    question_id:    str
    student_id:     str
    model_name:     str
    examiner_label: Optional[str]
    phase:          str
    assigned_score: Optional[float]
    max_score:      float
    basis_used:     Optional[str]
    reason:         Optional[str]
    matched_points: Optional[list]
    missing_points: Optional[list]
    score_changed:  Optional[bool]
    revision_reason: Optional[str]


class FinalScoreItem(BaseModel):
    question_id:      str
    student_id:       str
    final_score:      float
    max_score:        float
    grading_mode:     Optional[str]
    reasoning_summary: Optional[str]
    matched_points:   Optional[list]
    missing_points:   Optional[list]


# ══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _get_exam_or_404(exam_id: int, db: Session) -> Exam:
    exam = db.get(Exam, exam_id)
    if exam is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Exam {exam_id} not found.",
        )
    return exam


def _parse_json_list(raw: Optional[str]) -> Optional[list]:
    if not raw:
        return None
    try:
        val = json.loads(raw)
        return val if isinstance(val, list) else None
    except (json.JSONDecodeError, TypeError):
        return None


_VALID_MODELS = {"claude", "qwen", "deepseek", "openai"}


def _validate_models(models: list[str]) -> list[str]:
    """Normalise and validate model names; expand 'all' to individual-mode defaults."""
    normalised = [m.lower().strip() for m in models]
    if "all" in normalised:
        # "all" in individual mode → all four models
        return ["claude", "qwen", "deepseek", "openai"]
    invalid = [m for m in normalised if m not in _VALID_MODELS]
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Unknown model(s): {invalid}. "
                f"Accepted values: {sorted(_VALID_MODELS)} or 'all'."
            ),
        )
    return normalised


# ══════════════════════════════════════════════════════════════════════════════
#  POST /grading/start
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/start",
    response_model=GradingJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start a grading job for an exam",
)
def start_grading(
    body:             GradingStartRequest,
    background_tasks: BackgroundTasks,
    current_user:     User    = Depends(get_current_user),
    db:               Session = Depends(get_db),
):
    """
    Trigger the auto-grading pipeline for an exam.

    The endpoint validates pre-conditions, creates a GradingJob record,
    and immediately returns HTTP 202.  The actual grading runs in the
    background — poll GET /grading/status/{job_id} for progress.

    Pre-conditions checked:
      • Exam exists and the user has access to it
      • At least one ExamModelAnswer (answer key) exists
      • At least one StudentExtraction exists

    Model selection rules:
      • consensus_enabled=true  → Claude + OpenAI only (two-examiner consensus)
      • consensus_enabled=false → exactly the model(s) in selected_models
    """
    exam = _get_exam_or_404(body.exam_id, db)
    require_exam_access(exam, current_user)

    # ── Debug: log received payload ───────────────────────────────────────────
    print(f"[grading] ── /grading/start received ──────────────────────────────")
    print(f"[grading] received selected_models : {body.selected_models}")
    print(f"[grading] received consensus_enabled: {body.consensus_enabled}")

    # Validate model list
    selected = _validate_models(body.selected_models)

    # consensus_enabled forces Claude + OpenAI regardless of what was sent
    if body.consensus_enabled:
        selected = ["claude", "openai"]

    # models_to_check drives the API-key pre-flight — must match exactly what
    # will actually be called, nothing more.
    models_to_check = ["claude", "openai"] if body.consensus_enabled else selected

    print(f"[grading] final selected_models    : {selected}")
    print(f"[grading] final models_to_check    : {models_to_check}")
    print(f"[grading] final grading mode       : {'consensus' if body.consensus_enabled else 'individual'}")
    print(f"[grading] ───────────────────────────────────────────────────────────")

    # Check pre-conditions
    has_answer_key = (
        db.query(ExamModelAnswer)
        .filter(ExamModelAnswer.exam_id == body.exam_id)
        .first()
    ) is not None
    if not has_answer_key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "No answer key found for this exam. "
                "Upload and extract an answer key before grading."
            ),
        )

    has_extractions = (
        db.query(StudentExtraction)
        .filter(StudentExtraction.exam_id == body.exam_id)
        .first()
    ) is not None
    if not has_extractions:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "No student extractions found for this exam. "
                "Run student extraction before grading."
            ),
        )

    # ── Validate that every question has a non-zero score ────────────────────
    # Questions with score = 0 / NULL produce all-zero grading results which
    # is silent and confusing.  Reject the request here with a clear message.
    q_rows = (
        db.query(ExamModelAnswer)
        .filter(ExamModelAnswer.exam_id == body.exam_id)
        .order_by(ExamModelAnswer.question_number)
        .all()
    )
    invalid_questions: list[str] = []
    for row in q_rows:
        try:
            s = float(row.score or 0)
        except (TypeError, ValueError):
            s = 0.0
        if s <= 0:
            invalid_questions.append(f"Q{row.question_number}")
    if invalid_questions:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Please assign marks to all questions before starting grading. "
                "Questions with missing or zero marks cannot be graded. "
                f"Missing marks: {', '.join(invalid_questions)}"
            ),
        )

    # Verify required API keys are present before queuing the background job.
    # models_to_check was already computed and logged above.
    try:
        assert_keys_configured(models_to_check)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        )

    # ── LLM health check: send a live test prompt to each selected model ──────
    # Skipped automatically when the exam contains only MCQ/True-False questions
    # (check_llm_health_for_exam detects this from q_rows).
    # q_rows was already loaded above for the score-validation check.
    llm_ok, llm_err = check_llm_health_for_exam(
        selected_models   = models_to_check,
        consensus_enabled = body.consensus_enabled,
        q_rows            = q_rows,
    )
    if not llm_ok:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=llm_err,
        )

    # Scope log
    student_ids = body.student_ids or []
    if student_ids:
        print(
            f"[GRADING-SCOPE] mode=selected_students "
            f"count={len(student_ids)} student_ids={student_ids}"
        )
    else:
        print(f"[GRADING-SCOPE] mode=all_students")

    # Create the GradingJob record
    job = GradingJob(
        exam_id                      = body.exam_id,
        status                       = GradingJobStatus.pending,
        progress                     = 0,
        selected_models              = json.dumps(selected),
        grading_basis                = body.grading_basis,
        consensus_enabled            = body.consensus_enabled,
        general_grading_instructions = body.general_grading_instructions,
        student_ids                  = json.dumps(student_ids) if student_ids else None,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    print(
        f"[grading] job_id={job.id} created for exam_id={body.exam_id} "
        f"models={selected} consensus={body.consensus_enabled}"
    )

    # Queue background grading
    background_tasks.add_task(
        run_grading_background,
        job_id                        = job.id,
        exam_id                       = body.exam_id,
        selected_models               = selected,
        grading_basis                 = body.grading_basis,
        consensus_enabled             = body.consensus_enabled,
        general_grading_instructions  = body.general_grading_instructions,
        student_ids                   = student_ids,
    )

    return GradingJobResponse(
        job_id   = job.id,
        exam_id  = body.exam_id,
        status   = GradingJobStatus.pending,
        progress = 0,
        message  = (
            f"Grading started with "
            f"{'Collaborative Consensus' if body.consensus_enabled else 'Individual'} mode "
            f"using model(s): {', '.join(selected)}. "
            f"Poll GET /grading/status/{job.id} for progress."
        ),
    )



# ══════════════════════════════════════════════════════════════════════════════
#  GET /grading/status/{job_id}
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/status/{job_id}",
    response_model=GradingStatusResponse,
    summary="Poll the status of a grading job",
)
def get_grading_status(
    job_id:       int,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Return current status and progress of a grading job.

    status values: pending | running | completed | failed
    progress:      0–100 (percentage complete)
    """
    job = db.get(GradingJob, job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Grading job {job_id} not found.",
        )

    # Access check via exam
    exam = _get_exam_or_404(job.exam_id, db)
    require_exam_access(exam, current_user)

    return GradingStatusResponse(
        job_id            = job.id,
        exam_id           = job.exam_id,
        status            = job.status,
        progress          = job.progress,
        started_at        = job.started_at.isoformat() if job.started_at else None,
        completed_at      = job.completed_at.isoformat() if job.completed_at else None,
        error_message     = job.error_message,
        selected_models   = _parse_json_list(job.selected_models),
        grading_basis     = job.grading_basis,
        consensus_enabled = job.consensus_enabled,
    )


# ══════════════════════════════════════════════════════════════════════════════
#  GET /grading/jobs/{exam_id}
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/jobs/{exam_id}",
    response_model=List[GradingStatusResponse],
    summary="List all grading jobs for an exam",
)
def list_grading_jobs(
    exam_id:      int,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """Return all GradingJob records for the given exam, newest first."""
    exam = _get_exam_or_404(exam_id, db)
    require_exam_access(exam, current_user)

    jobs = (
        db.query(GradingJob)
        .filter(GradingJob.exam_id == exam_id)
        .order_by(GradingJob.created_at.desc())
        .all()
    )

    return [
        GradingStatusResponse(
            job_id            = j.id,
            exam_id           = j.exam_id,
            status            = j.status,
            progress          = j.progress,
            started_at        = j.started_at.isoformat() if j.started_at else None,
            completed_at      = j.completed_at.isoformat() if j.completed_at else None,
            error_message     = j.error_message,
            selected_models   = _parse_json_list(j.selected_models),
            grading_basis     = j.grading_basis,
            consensus_enabled = j.consensus_enabled,
        )
        for j in jobs
    ]


# ══════════════════════════════════════════════════════════════════════════════
#  GET /grading/results/{exam_id}/detail
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/results/{exam_id}/detail",
    summary="Per-model raw grading results for transparency reports",
)
def get_grading_detail(
    exam_id:      int,
    job_id:       Optional[int] = None,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Return detailed per-model, per-student grading results for an exam.

    Used to generate transparency / audit reports.  The UI shows only
    final scores; this endpoint exposes the full reasoning trail.

    Optionally filter by job_id (defaults to the latest completed job).
    """
    exam = _get_exam_or_404(exam_id, db)
    require_exam_access(exam, current_user)

    # Resolve job_id
    if job_id is None:
        latest = (
            db.query(GradingJob)
            .filter(
                GradingJob.exam_id == exam_id,
                GradingJob.status  == GradingJobStatus.completed,
            )
            .order_by(GradingJob.created_at.desc())
            .first()
        )
        if latest is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No completed grading job found for this exam.",
            )
        job_id = latest.id

    job = db.get(GradingJob, job_id)
    if job is None or job.exam_id != exam_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Grading job {job_id} not found for exam {exam_id}.",
        )

    # Load student identity map (extraction_id → student_id)
    extractions = (
        db.query(StudentExtraction)
        .filter(StudentExtraction.exam_id == exam_id)
        .all()
    )
    student_map: dict[int, str] = {
        e.id: (e.student_id_num or f"STU-{e.id:03d}")
        for e in extractions
    }

    # Load raw model results
    raw_results = (
        db.query(ModelGradingResult)
        .filter(ModelGradingResult.grading_job_id == job_id)
        .order_by(
            ModelGradingResult.question_id,
            ModelGradingResult.extraction_id,
            ModelGradingResult.model_name,
            ModelGradingResult.phase,
        )
        .all()
    )

    model_items = [
        ModelResultItem(
            question_id     = r.question_id,
            student_id      = student_map.get(r.extraction_id, str(r.extraction_id)),
            model_name      = r.model_name,
            examiner_label  = r.examiner_label,
            phase           = r.phase,
            assigned_score  = float(r.assigned_score) if r.assigned_score is not None else None,
            max_score       = float(r.max_score),
            basis_used      = r.basis_used,
            reason          = r.reason,
            matched_points  = _parse_json_list(r.matched_points),
            missing_points  = _parse_json_list(r.missing_points),
            score_changed   = r.score_changed,
            revision_reason = r.revision_reason,
        )
        for r in raw_results
    ]

    # Load final scores
    final_rows = (
        db.query(GradingFinalScore)
        .filter(GradingFinalScore.grading_job_id == job_id)
        .order_by(GradingFinalScore.question_id, GradingFinalScore.extraction_id)
        .all()
    )

    final_items = [
        FinalScoreItem(
            question_id       = f.question_id,
            student_id        = student_map.get(f.extraction_id, str(f.extraction_id)),
            final_score       = float(f.final_score),
            max_score         = float(f.max_score),
            grading_mode      = f.grading_mode,
            reasoning_summary = f.reasoning_summary,
            matched_points    = _parse_json_list(f.matched_points),
            missing_points    = _parse_json_list(f.missing_points),
        )
        for f in final_rows
    ]

    return {
        "job_id":         job_id,
        "exam_id":        exam_id,
        "grading_mode":   "consensus" if job.consensus_enabled else "individual",
        "selected_models": _parse_json_list(job.selected_models),
        "model_results":  [item.model_dump() for item in model_items],
        "final_scores":   [item.model_dump() for item in final_items],
    }


# ══════════════════════════════════════════════════════════════════════════════
#  GET /grading/export/{exam_id}
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/export/{exam_id}",
    summary="Download a grading report (CSV, Excel, or PDF)",
)
def export_grading_report(
    exam_id:      int,
    format:       str          = Query("csv", description="csv | xlsx | pdf"),
    job_id:       Optional[int] = None,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Download a grading report for the given exam.

    Defaults to the latest completed grading job.  Pass job_id to target a
    specific job.

    format values:
      csv   — UTF-8 CSV (BOM-prefixed for Excel compatibility)
      xlsx  — Excel workbook with a summary sheet and (in consensus mode) an
              Examiner Detail transparency sheet
      pdf   — Landscape A4 PDF table
    """
    exam = _get_exam_or_404(exam_id, db)
    require_exam_access(exam, current_user)

    fmt = format.lower().strip()
    if fmt not in ("csv", "xlsx", "pdf"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="format must be csv, xlsx, or pdf.",
        )

    try:
        if fmt == "csv":
            content      = generate_csv(exam_id, db, job_id)
            media_type   = "text/csv"
            filename     = f"grading_report_{exam_id}.csv"
        elif fmt == "xlsx":
            content      = generate_excel(exam_id, db, job_id)
            media_type   = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            filename     = f"grading_report_{exam_id}.xlsx"
        else:  # pdf
            content      = generate_pdf(exam_id, db, job_id)
            media_type   = "application/pdf"
            filename     = f"grading_report_{exam_id}.pdf"
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        )

    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(content)),
        },
    )


# ══════════════════════════════════════════════════════════════════════════════
#  POST /grading/finalize/{job_id}
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/finalize/{job_id}",
    summary="Rebuild exam results from stored scores (no LLM calls)",
)
def finalize_job(
    job_id:       int,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Repair a grading job that crashed after all LLM scores were written but
    before (or during) the aggregation step.

    Reads existing GradingFinalScore rows for *job_id* and rebuilds:
      • ExamResult rows (per student)
      • StudentQuestionScore rows (per student × question)
      • Exam.avg_score / Exam.graded_count aggregate fields

    No LLM calls are made.  Selective mode is honoured automatically — if
    the original job targeted a subset of students, only those students'
    results are rebuilt; other students' rows are left untouched.

    Returns 404 if the job does not exist.
    Returns 422 if no GradingFinalScore rows exist for the job.
    """
    job = db.get(GradingJob, job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"GradingJob {job_id} not found.",
        )

    require_exam_access(db.get(Exam, job.exam_id), current_user)

    try:
        finalize_job_results(job_id=job_id, exam_id=job.exam_id, db=db)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )

    return {
        "job_id":  job_id,
        "exam_id": job.exam_id,
        "status":  "completed",
        "message": "Exam results rebuilt successfully from stored grading scores.",
    }


# ══════════════════════════════════════════════════════════════════════════════
#  POST /grading/confirm/{job_id}
# ══════════════════════════════════════════════════════════════════════════════

class GradingConfirmRequest(BaseModel):
    apply: bool = Field(
        ...,
        description=(
            "true  → apply new grading results, replacing existing results for "
            "the selected students and activating the new report. "
            "false → keep previous results unchanged; the new grading run is "
            "stored in Report History as a draft but not applied."
        ),
    )


@router.post(
    "/confirm/{job_id}",
    summary="Confirm or decline applying new grading results over existing ones",
)
def confirm_grading(
    job_id:       int,
    payload:      GradingConfirmRequest,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Resolve a job that is paused at ``awaiting_confirmation``.

    Called after the instructor responds to the "students already have results"
    modal in the UI.

    ``apply=true``  — write new ExamResult / StudentQuestionScore rows for the
                      selected students (overwriting existing ones), activate
                      the new GradingReport, mark the previous one "replaced".

    ``apply=false`` — leave all existing results untouched. The draft
                      GradingReport for this job remains in Report History but
                      is never activated.

    In both cases the job is marked ``completed`` so the UI can advance.
    """
    job = db.get(GradingJob, job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"GradingJob {job_id} not found.",
        )

    require_exam_access(db.get(Exam, job.exam_id), current_user)

    try:
        if payload.apply:
            apply_job_results(job_id=job_id, db=db)
        else:
            decline_job_results(job_id=job_id, db=db)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )

    return {
        "job_id":  job_id,
        "exam_id": job.exam_id,
        "applied": payload.apply,
        "status":  "completed",
        "message": (
            "New grading results applied successfully."
            if payload.apply
            else "Previous results kept. New grading run saved as draft in Report History."
        ),
    }
