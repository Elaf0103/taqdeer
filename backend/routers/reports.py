"""
Reports Router — /reports
=========================
Endpoints for grading report version management.

Each completed grading job creates one GradingReport record.
Multiple versions can exist per exam; exactly one may be "active".

Endpoints
---------
GET  /reports/                        — list reports for an exam
POST /reports/{report_id}/activate    — make this report the active one
DELETE /reports/{report_id}           — delete a report
GET  /reports/{report_id}/export      — download CSV, Excel, or PDF on demand
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db
from deps import get_current_user
from models import Exam, GradingJob, GradingReport, User
from permissions import require_exam_access
from services.export_service import generate_csv, generate_excel, generate_pdf

router = APIRouter(prefix="/reports", tags=["Reports"])


# ══════════════════════════════════════════════════════════════════════════════
#  Schemas
# ══════════════════════════════════════════════════════════════════════════════

class ReportOut(BaseModel):
    id:             int
    exam_id:        int
    grading_job_id: Optional[int]
    version_number: int
    scope:          str
    student_count:  Optional[int]
    average_score:  Optional[float]
    status:         str
    is_active:      bool
    created_at:     datetime
    approved_at:    Optional[datetime]

    class Config:
        from_attributes = True


# ══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _get_report_or_404(report_id: int, db: Session) -> GradingReport:
    r = db.get(GradingReport, report_id)
    if r is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Report {report_id} not found.",
        )
    return r


# ══════════════════════════════════════════════════════════════════════════════
#  GET /reports/?exam_id=X
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/", response_model=List[ReportOut], summary="List grading reports for an exam")
def list_reports(
    exam_id:      int,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Return all GradingReport records for the given exam, newest first.
    Reports remain visible regardless of their status.
    """
    exam = db.get(Exam, exam_id)
    if exam is None:
        raise HTTPException(status_code=404, detail=f"Exam {exam_id} not found.")
    require_exam_access(exam, current_user)

    rows = (
        db.query(GradingReport)
        .filter(GradingReport.exam_id == exam_id)
        .order_by(GradingReport.version_number.desc())
        .all()
    )
    return rows


# ══════════════════════════════════════════════════════════════════════════════
#  POST /reports/{report_id}/activate
# ══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/{report_id}/activate",
    response_model=ReportOut,
    summary="Make this report the active one for its exam",
)
def activate_report(
    report_id:    int,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Make *report_id* the active report for its exam.

    Any previously active report is marked as "replaced".
    The target report's status is set to "active".
    """
    report = _get_report_or_404(report_id, db)
    exam   = db.get(Exam, report.exam_id)
    require_exam_access(exam, current_user)

    if report.is_active:
        return report   # already active — idempotent

    # Mark every previously active report for this exam as "replaced"
    prev_active = (
        db.query(GradingReport)
        .filter(
            GradingReport.exam_id  == report.exam_id,
            GradingReport.is_active == True,
        )
        .all()
    )
    for prev in prev_active:
        prev.is_active = False
        prev.status    = "replaced"

    report.is_active   = True
    report.status      = "active"
    report.approved_at = datetime.utcnow()
    db.commit()
    db.refresh(report)
    return report


# ══════════════════════════════════════════════════════════════════════════════
#  DELETE /reports/{report_id}
# ══════════════════════════════════════════════════════════════════════════════

@router.delete(
    "/{report_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a grading report record",
)
def delete_report(
    report_id:    int,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Delete a GradingReport record.

    If the deleted report was the active one, the next most-recent report
    (by version_number) is automatically promoted to active.
    Deletion does NOT delete the underlying grading_job or exam_results.
    """
    report = _get_report_or_404(report_id, db)
    exam   = db.get(Exam, report.exam_id)
    require_exam_access(exam, current_user)

    was_active = report.is_active
    exam_id    = report.exam_id

    db.delete(report)
    db.flush()

    if was_active:
        # Promote the next most-recent report for this exam (if any)
        next_report = (
            db.query(GradingReport)
            .filter(GradingReport.exam_id == exam_id)
            .order_by(GradingReport.version_number.desc())
            .first()
        )
        if next_report:
            next_report.is_active   = True
            next_report.status      = "active"
            next_report.approved_at = next_report.approved_at or datetime.utcnow()

    db.commit()


# ══════════════════════════════════════════════════════════════════════════════
#  GET /reports/{report_id}/export?format=csv|xlsx|pdf
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/{report_id}/export",
    summary="Download a grading report (CSV, Excel, or PDF) for a specific version",
)
def export_report(
    report_id:    int,
    format:       str          = Query("csv", description="csv | xlsx | pdf"),
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Generate and download the export for a specific report version.
    Delegates to export_service using the report's grading_job_id so the
    output reflects exactly what was graded in that run.
    """
    report = _get_report_or_404(report_id, db)
    exam   = db.get(Exam, report.exam_id)
    require_exam_access(exam, current_user)

    fmt = format.lower().strip()
    if fmt not in ("csv", "xlsx", "pdf"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="format must be csv, xlsx, or pdf.",
        )

    try:
        if fmt == "csv":
            content    = generate_csv(report.exam_id, db, report.grading_job_id)
            media_type = "text/csv"
            filename   = f"grading_report_v{report.version_number}_{report.exam_id}.csv"
        elif fmt == "xlsx":
            content    = generate_excel(report.exam_id, db, report.grading_job_id)
            media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            filename   = f"grading_report_v{report.version_number}_{report.exam_id}.xlsx"
        else:
            content    = generate_pdf(report.exam_id, db, report.grading_job_id)
            media_type = "application/pdf"
            filename   = f"grading_report_v{report.version_number}_{report.exam_id}.pdf"
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))

    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(content)),
        },
    )
