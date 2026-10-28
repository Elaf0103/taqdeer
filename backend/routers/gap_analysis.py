"""
Gap Analysis Router — /gap-analysis
====================================
Endpoint for section-level learning gap analysis.

GET /gap-analysis/{exam_id}
  Returns a structured status envelope:

    { "status": "ready",      "analysis": {...} }   — cached result available
    { "status": "processing"                    }   — grading/analysis still running
    { "status": "failed"                        }   — analysis could not be generated

  Status determination:
    1. If exam_gap_analysis row exists and parses cleanly → "ready"
    2. If a GradingJob for this exam is pending/running  → "processing"
    3. Otherwise (no cache, no active job)               → trigger once and return
       result, or "failed" if trigger raises.

POST /gap-analysis/{exam_id}/retry
  Forces a fresh run of gap analysis regardless of cached state.
  Useful after a "failed" status or corrupt cache.

Authentication: requires a valid JWT.  Instructors can only view exams
they own; admins can view any exam.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from database import get_db
from deps import get_current_user
from models import Exam, ExamGapAnalysis, ExamResult, GradeStatus, GradingJob, GradingJobStatus, User
from permissions import require_exam_access
from services.gap_analysis_engine import run_gap_analysis, trigger_gap_analysis

router = APIRouter(prefix="/gap-analysis", tags=["Gap Analysis"])


_MIN_STUDENTS_FOR_INSIGHTS = 1  # gate: at least 1 fully-approved result required


def _is_grading_active(db: Session, exam_id: int) -> bool:
    """Return True if a grading job for this exam is currently pending or running."""
    return (
        db.query(GradingJob)
        .filter(
            GradingJob.exam_id == exam_id,
            GradingJob.status.in_([GradingJobStatus.pending, GradingJobStatus.running]),
        )
        .first()
    ) is not None


def _get_readiness(db: Session, exam: Exam) -> dict | None:
    """
    Determine whether this exam's grading workflow is complete enough to
    generate meaningful learning gap insights.

    Returns a readiness dict describing the current workflow stage when the
    exam is NOT yet ready, or None when analysis should proceed normally.

    Stages
    ------
    1 — No grading started (no results, no active job)
    2 — Grading in progress (active job running, or only partial results exist)
    3 — Instructor review pending (all results graded, but none approved yet)

    None → all results approved → proceed with analysis
    """
    total_results: int = (
        db.query(ExamResult)
        .filter(ExamResult.exam_id == exam.id)
        .count()
    )
    is_active = _is_grading_active(db, exam.id)

    if total_results == 0:
        # Nothing graded at all
        stage = 2 if is_active else 1
        return {
            "stage": stage,
            "reason": "grading_in_progress" if is_active else "no_grading_started",
            "graded_count": 0,
            "student_count": exam.student_count,
            "approved_count": 0,
        }

    approved_count: int = (
        db.query(ExamResult)
        .filter(
            ExamResult.exam_id == exam.id,
            ExamResult.status.in_([GradeStatus.approved, GradeStatus.overridden]),
        )
        .count()
    )

    if approved_count >= total_results:
        return None  # All results approved — proceed

    if is_active:
        return {
            "stage": 2,
            "reason": "grading_in_progress",
            "graded_count": total_results,
            "student_count": exam.student_count,
            "approved_count": approved_count,
        }

    # Grading done, review still pending
    return {
        "stage": 3,
        "reason": "review_pending",
        "graded_count": total_results,
        "student_count": exam.student_count,
        "approved_count": approved_count,
    }


@router.get("/{exam_id}")
def get_gap_analysis(
    exam_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Return the learning gap analysis status + (when ready) the analysis payload.

    Response shape:
      { "status": "ready",      "analysis": { exam_id, exam_name, ... } }
      { "status": "processing"                                           }
      { "status": "failed"                                               }
    """
    # ── Access check ──────────────────────────────────────────────────────────
    exam = db.get(Exam, exam_id)
    if exam is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Exam not found.")
    require_exam_access(exam, current_user)

    # ── Readiness gate ────────────────────────────────────────────────────────
    # Learning insights are only meaningful once all intended student papers are
    # graded and the instructor has approved the final results.  Return a
    # structured "not_ready" envelope so the frontend can display the correct
    # workflow-stage UI instead of partial or speculative analytics.
    readiness = _get_readiness(db, exam)
    if readiness is not None:
        print(
            f"[GAP-ANALYSIS-STATUS] exam_id={exam_id} status=not_ready "
            f"stage={readiness['stage']} reason={readiness['reason']}"
        )
        return {"status": "not_ready", "readiness": readiness}

    # ── 1. Cache-first lookup ─────────────────────────────────────────────────
    cached_row: ExamGapAnalysis | None = (
        db.query(ExamGapAnalysis)
        .filter(ExamGapAnalysis.exam_id == exam_id)
        .first()
    )

    if cached_row:
        try:
            analysis = json.loads(cached_row.analysis_json)
            analysis["cached"] = True
            analysis["small_sample"] = (analysis.get("n_students", 0) < 5)
            print(f"[GAP-ANALYSIS-STATUS] exam_id={exam_id} status=ready")
            return {"status": "ready", "analysis": analysis}
        except (json.JSONDecodeError, ValueError):
            # Corrupt cache — fall through, treat as a miss
            pass

    # ── 2. Is grading still in progress? ─────────────────────────────────────
    if _is_grading_active(db, exam_id):
        print(f"[GAP-ANALYSIS-STATUS] exam_id={exam_id} status=processing")
        return {"status": "processing"}

    # ── 3. Cache miss + no active job: trigger once and store ─────────────────
    # Covers exams graded before this feature was deployed, or a corrupt cache.
    try:
        trigger_gap_analysis(exam_id)   # opens its own SessionLocal

        # Refresh session to pick up the new row
        db.expire_all()
        fresh_row = (
            db.query(ExamGapAnalysis)
            .filter(ExamGapAnalysis.exam_id == exam_id)
            .first()
        )
        if fresh_row:
            analysis = json.loads(fresh_row.analysis_json)
            analysis["cached"] = False
            analysis["small_sample"] = (analysis.get("n_students", 0) < 5)
            print(f"[GAP-ANALYSIS-STATUS] exam_id={exam_id} status=ready")
            return {"status": "ready", "analysis": analysis}

        # trigger_gap_analysis silently failed — direct call as last resort
        analysis = run_gap_analysis(db, exam_id)
        analysis["cached"] = False
        analysis["small_sample"] = (analysis.get("n_students", 0) < 5)
        print(f"[GAP-ANALYSIS-STATUS] exam_id={exam_id} status=ready")
        return {"status": "ready", "analysis": analysis}

    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )
    except Exception:
        print(f"[GAP-ANALYSIS-STATUS] exam_id={exam_id} status=failed")
        return {"status": "failed"}


@router.post("/{exam_id}/retry")
def retry_gap_analysis(
    exam_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Force a fresh gap analysis run, clearing any cached or corrupt result.
    Returns the same status envelope as GET /{exam_id}.
    """
    exam = db.get(Exam, exam_id)
    if exam is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Exam not found.")
    require_exam_access(exam, current_user)

    # Clear stale cache so trigger_gap_analysis writes a fresh row
    stale = (
        db.query(ExamGapAnalysis)
        .filter(ExamGapAnalysis.exam_id == exam_id)
        .first()
    )
    if stale:
        db.delete(stale)
        db.commit()

    try:
        trigger_gap_analysis(exam_id)

        db.expire_all()
        fresh_row = (
            db.query(ExamGapAnalysis)
            .filter(ExamGapAnalysis.exam_id == exam_id)
            .first()
        )
        if fresh_row:
            analysis = json.loads(fresh_row.analysis_json)
            analysis["cached"] = False
            analysis["small_sample"] = (analysis.get("n_students", 0) < 5)
            print(f"[GAP-ANALYSIS-STATUS] exam_id={exam_id} status=ready (retry)")
            return {"status": "ready", "analysis": analysis}

        analysis = run_gap_analysis(db, exam_id)
        analysis["cached"] = False
        analysis["small_sample"] = (analysis.get("n_students", 0) < 5)
        print(f"[GAP-ANALYSIS-STATUS] exam_id={exam_id} status=ready (retry)")
        return {"status": "ready", "analysis": analysis}

    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )
    except Exception:
        print(f"[GAP-ANALYSIS-STATUS] exam_id={exam_id} status=failed (retry)")
        return {"status": "failed"}
