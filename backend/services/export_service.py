"""
Export Service
==============
Generate grading reports in CSV, Excel (.xlsx), and PDF formats.

Report structure
----------------
SUMMARY  — one row per student: total score, max, percentage
DETAIL   — one row per student × question with:
             Question ID, Question Text, Final Score, Max Score,
             Basis Used, Reason, Matched Points, Missing Points

In INDIVIDUAL / DETERMINISTIC mode the detail section also includes:
  Examiner N Score / Reason / Matched / Missing (one column-group per model)

In CONSENSUS mode the detail section also includes:
  Examiner N Initial Score / Reason / Matched / Missing
  Examiner N Final Score / Score Changed / Revision Reason

Model names (claude/qwen/deepseek) are NEVER written to any output.
Examiners are always labelled "Examiner 1", "Examiner 2", "Examiner 3", …

Public API
----------
  generate_csv(exam_id, db, job_id=None)   → bytes
  generate_excel(exam_id, db, job_id=None) → bytes
  generate_pdf(exam_id, db, job_id=None)   → bytes
"""

from __future__ import annotations

import csv
import io
import json
from typing import Optional

from sqlalchemy.orm import Session

from models import (
    Exam, ExamModelAnswer,
    GradingFinalScore, GradingJob, GradingJobStatus,
    ModelGradingResult, GradingPhase,
    StudentExtraction,
)


# ══════════════════════════════════════════════════════════════════════════════
#  Internal helpers
# ══════════════════════════════════════════════════════════════════════════════

def _resolve_job(job_id: Optional[int], exam_id: int, db: Session) -> GradingJob:
    if job_id is None:
        job = (
            db.query(GradingJob)
            .filter(
                GradingJob.exam_id == exam_id,
                GradingJob.status  == GradingJobStatus.completed,
            )
            .order_by(GradingJob.created_at.desc())
            .first()
        )
        if job is None:
            raise ValueError("No completed grading job found for this exam.")
        return job
    job = db.get(GradingJob, job_id)
    if job is None or job.exam_id != exam_id:
        raise ValueError(f"Grading job {job_id} not found for exam {exam_id}.")
    return job


def _parse_list(raw: Optional[str]) -> list[str]:
    """Parse a JSON array stored as a string; fall back to empty list."""
    if not raw:
        return []
    try:
        val = json.loads(raw)
        if isinstance(val, list):
            return [str(x) for x in val]
        return [str(val)]
    except (json.JSONDecodeError, TypeError):
        return [str(raw)] if raw else []


def _fmt_list(items: list[str]) -> str:
    return "; ".join(items) if items else ""


def _build_question_text_lookup(exam_id: int, db: Session) -> dict[str, str]:
    """Return {question_id_upper: question_text} from ExamModelAnswer rows."""
    rows = (
        db.query(ExamModelAnswer)
        .filter(ExamModelAnswer.exam_id == exam_id)
        .all()
    )
    lookup: dict[str, str] = {}
    for row in rows:
        qnum = f"Q{row.question_number}"
        ma   = row.model_answer or ""
        if ma.startswith('{"__sub__":'):
            try:
                parsed   = json.loads(ma)
                sub_list = parsed.get("__sub__", [])
                for entry in sub_list:
                    label = entry.get("label", "a")
                    lookup[f"{qnum}{label}".upper()] = entry.get(
                        "question_text", row.question_text or ""
                    )
            except (json.JSONDecodeError, ValueError):
                lookup[qnum.upper()] = row.question_text or ""
        else:
            lookup[qnum.upper()] = row.question_text or ""
    return lookup


# ══════════════════════════════════════════════════════════════════════════════
#  Data assembly
# ══════════════════════════════════════════════════════════════════════════════

def _build_report_data(job: GradingJob, exam_id: int, db: Session) -> dict:
    """
    Assemble all data needed for any export format.

    Returned structure
    ------------------
    {
      "exam_name":    str,
      "is_consensus": bool,
      "columns":      list[str],          # question_ids in sorted order
      "students":     list[student_dict],
    }

    student_dict
    ------------
    {
      "student_id":   str,
      "student_name": str,
      "total_score":  float,  # capped at max_total
      "max_total":    float,
      "percentage":   float,
      "questions":    dict[qid → q_entry],
    }

    q_entry (always)
    ----------------
    {
      "question_text":  str,
      "final_score":    float,  # capped at max_score
      "max_score":      float,
      "basis_used":     str,
      "reason":         str,
      "matched_points": list[str],
      "missing_points": list[str],
    }

    q_entry (individual / deterministic mode only)
    ----------------------------------------------
      "individual_detail": [
          {"examiner_label": str, "score": float,
           "reason": str, "matched_points": list, "missing_points": list}
      ]

    q_entry (consensus mode only)
    ------------------------------
      "consensus_detail": {
          "initial": [
              {"examiner_label": str, "score": float,
               "reason": str, "matched_points": list, "missing_points": list}
          ],
          "revised": [
              {"examiner_label": str, "score": float,
               "reason": str, "score_changed": bool, "revision_reason": str}
          ],
      }
    """
    exam = db.get(Exam, exam_id)
    exam_name        = exam.name if exam else f"Exam {exam_id}"
    exam_total_marks = float(exam.total_marks) if (exam and exam.total_marks) else 0.0

    is_consensus = bool(job.consensus_enabled)

    q_text_map = _build_question_text_lookup(exam_id, db)

    # Student identity map: extraction_id → {student_id, student_name}
    extractions = (
        db.query(StudentExtraction)
        .filter(StudentExtraction.exam_id == exam_id)
        .all()
    )
    student_map: dict[int, dict] = {
        e.id: {
            "student_id":   e.student_id_num or f"STU-{e.id:03d}",
            "student_name": e.student_name or "",
        }
        for e in extractions
    }

    # Final scores — fetch all rows, then deduplicate: keep newest per
    # (extraction_id, question_id) in case of re-grades.
    final_rows_raw = (
        db.query(GradingFinalScore)
        .filter(GradingFinalScore.grading_job_id == job.id)
        .order_by(
            GradingFinalScore.extraction_id,
            GradingFinalScore.question_id,
            GradingFinalScore.id.desc(),
        )
        .all()
    )
    seen_final: set[tuple] = set()
    final_rows: list[GradingFinalScore] = []
    for row in final_rows_raw:
        key = (row.extraction_id, row.question_id)
        if key not in seen_final:
            seen_final.add(key)
            final_rows.append(row)

    # ── Export completeness guard ─────────────────────────────────────────────
    # Compare grading_final_scores question IDs against the expected question IDs
    # from exam_model_answers. Log a warning if any questions are missing so
    # incomplete exports are never silently produced.
    expected_qids = set(q_text_map.keys())
    exported_qids = {row.question_id for row in final_rows}
    missing_qids  = expected_qids - exported_qids
    if missing_qids:
        missing_sorted = sorted(missing_qids)
        print(
            f"[EXPORT-WARNING] exam_id={exam_id} job_id={job.id} "
            f"expected_questions_count={len(expected_qids)} "
            f"exported_questions_count={len(exported_qids)} "
            f"missing_final_scores={', '.join(missing_sorted)}"
        )
    else:
        print(
            f"[EXPORT-OK] exam_id={exam_id} job_id={job.id} "
            f"all {len(expected_qids)} expected question(s) present in grading_final_scores"
        )

    # Per-model results for all relevant phases
    model_rows = (
        db.query(ModelGradingResult)
        .filter(
            ModelGradingResult.grading_job_id == job.id,
            ModelGradingResult.phase.in_([
                GradingPhase.individual,
                GradingPhase.deterministic,
                GradingPhase.consensus_initial,
                GradingPhase.consensus_revised,
            ]),
        )
        .order_by(ModelGradingResult.examiner_label, ModelGradingResult.id)
        .all()
    )

    # Index model rows: (extraction_id, question_id, phase) → [ModelGradingResult, …]
    model_lookup: dict[tuple, list] = {}
    for r in model_rows:
        key = (r.extraction_id, r.question_id, r.phase)
        model_lookup.setdefault(key, []).append(r)

    # All question IDs in natural sort order (Q1, Q1A, Q1B, Q2, …)
    all_question_ids = sorted(
        {f.question_id for f in final_rows},
        key=lambda q: (len(q), q),
    )

    # Per-question max score (first seen per qid)
    q_max: dict[str, float] = {}
    for f in final_rows:
        if f.question_id not in q_max:
            q_max[f.question_id] = float(f.max_score)

    # Index final rows: extraction_id → {question_id → row}
    by_student: dict[int, dict[str, GradingFinalScore]] = {}
    for f in final_rows:
        by_student.setdefault(f.extraction_id, {})[f.question_id] = f

    students_out: list[dict] = []

    for ext_id, info in student_map.items():
        q_data: dict[str, dict] = {}
        total   = 0.0
        max_tot = 0.0

        for qid in all_question_ids:
            frow      = by_student.get(ext_id, {}).get(qid)
            q_max_val = q_max.get(qid, 0.0)

            if frow:
                fscore  = min(float(frow.final_score), q_max_val)
                fmax    = float(frow.max_score)
                basis   = frow.grading_mode or ""
                reason  = frow.reasoning_summary or ""
                matched = _parse_list(frow.matched_points)
                missing = _parse_list(frow.missing_points)
            else:
                fscore  = 0.0
                fmax    = q_max_val
                basis   = ""
                reason  = ""
                matched = []
                missing = []

            q_entry: dict = {
                "question_text":  q_text_map.get(qid, ""),
                "final_score":    fscore,
                "max_score":      fmax,
                "basis_used":     basis,
                "reason":         reason,
                "matched_points": matched,
                "missing_points": missing,
            }

            if is_consensus:
                init_rows = model_lookup.get((ext_id, qid, GradingPhase.consensus_initial), [])
                rev_rows  = model_lookup.get((ext_id, qid, GradingPhase.consensus_revised), [])
                q_entry["consensus_detail"] = {
                    "initial": [
                        {
                            "examiner_label":  r.examiner_label or "Examiner",
                            "score":           float(r.assigned_score or 0),
                            "reason":          r.reason or "",
                            "matched_points":  _parse_list(r.matched_points),
                            "missing_points":  _parse_list(r.missing_points),
                        }
                        for r in init_rows
                    ],
                    "revised": [
                        {
                            "examiner_label":  r.examiner_label or "Examiner",
                            "score":           float(r.assigned_score or 0),
                            "reason":          r.reason or "",
                            "score_changed":   bool(r.score_changed),
                            "revision_reason": r.revision_reason or "",
                        }
                        for r in rev_rows
                    ],
                }
            else:
                ind_rows: list = []
                for ph in (GradingPhase.individual, GradingPhase.deterministic):
                    ind_rows.extend(model_lookup.get((ext_id, qid, ph), []))
                q_entry["individual_detail"] = [
                    {
                        "examiner_label":  r.examiner_label or r.model_name or "Examiner",
                        "score":           float(r.assigned_score or 0),
                        "reason":          r.reason or "",
                        "matched_points":  _parse_list(r.matched_points),
                        "missing_points":  _parse_list(r.missing_points),
                    }
                    for r in ind_rows
                ]

            q_data[qid] = q_entry
            total   += fscore
            max_tot += fmax

        # Cap total score
        if exam_total_marks > 0:
            total = min(total, exam_total_marks)
        elif max_tot > 0:
            total = min(total, max_tot)

        pct = round(total / max_tot * 100, 1) if max_tot > 0 else 0.0

        students_out.append({
            "student_id":   info["student_id"],
            "student_name": info["student_name"],
            "total_score":  round(total, 2),
            "max_total":    round(max_tot, 2),
            "percentage":   pct,
            "questions":    q_data,
        })

    students_out.sort(key=lambda s: s["student_id"])

    return {
        "exam_name":    exam_name,
        "is_consensus": is_consensus,
        "columns":      all_question_ids,
        "students":     students_out,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  CSV
# ══════════════════════════════════════════════════════════════════════════════

def _max_examiners(data: dict, phase_key: str) -> int:
    """Count the maximum number of examiner entries across all students/questions."""
    mx = 0
    for s in data["students"]:
        for qid in data["columns"]:
            q = s["questions"].get(qid, {})
            if phase_key == "individual":
                mx = max(mx, len(q.get("individual_detail", [])))
            else:
                cd = q.get("consensus_detail", {})
                mx = max(mx, len(cd.get("initial", [])), len(cd.get("revised", [])))
    return max(mx, 1)


def generate_csv(
    exam_id: int,
    db: Session,
    job_id: Optional[int] = None,
) -> bytes:
    job  = _resolve_job(job_id, exam_id, db)
    data = _build_report_data(job, exam_id, db)

    buf    = io.StringIO()
    writer = csv.writer(buf)

    # ── SUMMARY SECTION ───────────────────────────────────────────────────────
    writer.writerow(["=== SUMMARY ==="])
    writer.writerow(["Student ID", "Student Name", "Total Score", "Max Score", "Percentage"])
    for s in data["students"]:
        writer.writerow([
            s["student_id"],
            s["student_name"],
            s["total_score"],
            s["max_total"],
            f"{s['percentage']}%",
        ])

    writer.writerow([])

    # ── DETAIL SECTION ────────────────────────────────────────────────────────
    writer.writerow(["=== QUESTION DETAIL ==="])

    base_cols = [
        "Student ID", "Student Name",
        "Question ID", "Question Text",
        "Final Score", "Max Score", "Basis Used",
        "Reason", "Matched Points", "Missing Points",
    ]

    if data["is_consensus"]:
        n_ex = _max_examiners(data, "consensus")
        ex_cols: list[str] = []
        for i in range(1, n_ex + 1):
            ex_cols += [
                f"Examiner {i} Initial Score",
                f"Examiner {i} Initial Reason",
                f"Examiner {i} Initial Matched",
                f"Examiner {i} Initial Missing",
                f"Examiner {i} Final Score",
                f"Examiner {i} Score Changed",
                f"Examiner {i} Revision Reason",
            ]
        writer.writerow(base_cols + ex_cols)

        for s in data["students"]:
            for qid in data["columns"]:
                q  = s["questions"].get(qid, {})
                cd = q.get("consensus_detail", {})
                init_by = {r["examiner_label"]: r for r in cd.get("initial", [])}
                rev_by  = {r["examiner_label"]: r for r in cd.get("revised", [])}
                labels  = sorted(set(list(init_by) + list(rev_by)))
                while len(labels) < n_ex:
                    labels.append(f"Examiner {len(labels) + 1}")

                row: list = [
                    s["student_id"], s["student_name"],
                    qid, q.get("question_text", ""),
                    q.get("final_score", 0), q.get("max_score", 0),
                    q.get("basis_used", ""), q.get("reason", ""),
                    _fmt_list(q.get("matched_points", [])),
                    _fmt_list(q.get("missing_points", [])),
                ]
                for lbl in labels:
                    ir = init_by.get(lbl, {})
                    rr = rev_by.get(lbl, {})
                    row += [
                        ir.get("score", ""),
                        ir.get("reason", ""),
                        _fmt_list(ir.get("matched_points", [])),
                        _fmt_list(ir.get("missing_points", [])),
                        rr.get("score", ""),
                        ("Yes" if rr.get("score_changed") else "No") if rr else "",
                        rr.get("revision_reason", ""),
                    ]
                writer.writerow(row)

    else:
        n_ex = _max_examiners(data, "individual")
        ex_cols = []
        for i in range(1, n_ex + 1):
            ex_cols += [
                f"Examiner {i} Score",
                f"Examiner {i} Reason",
                f"Examiner {i} Matched",
                f"Examiner {i} Missing",
            ]
        writer.writerow(base_cols + ex_cols)

        for s in data["students"]:
            for qid in data["columns"]:
                q   = s["questions"].get(qid, {})
                ind = q.get("individual_detail", [])
                row = [
                    s["student_id"], s["student_name"],
                    qid, q.get("question_text", ""),
                    q.get("final_score", 0), q.get("max_score", 0),
                    q.get("basis_used", ""), q.get("reason", ""),
                    _fmt_list(q.get("matched_points", [])),
                    _fmt_list(q.get("missing_points", [])),
                ]
                for i in range(n_ex):
                    d = ind[i] if i < len(ind) else {}
                    row += [
                        d.get("score", ""),
                        d.get("reason", ""),
                        _fmt_list(d.get("matched_points", [])),
                        _fmt_list(d.get("missing_points", [])),
                    ]
                writer.writerow(row)

    return buf.getvalue().encode("utf-8-sig")


# ══════════════════════════════════════════════════════════════════════════════
#  Excel (.xlsx)
# ══════════════════════════════════════════════════════════════════════════════

def generate_excel(
    exam_id: int,
    db: Session,
    job_id: Optional[int] = None,
) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    job  = _resolve_job(job_id, exam_id, db)
    data = _build_report_data(job, exam_id, db)

    wb = Workbook()

    # ── Shared styles ─────────────────────────────────────────────────────────
    H_FILL  = PatternFill("solid", fgColor="2B4A6F")
    H_FONT  = Font(bold=True, color="FFFFFF", size=10)
    SH_FILL = PatternFill("solid", fgColor="4A7BAF")
    SH_FONT = Font(bold=True, color="FFFFFF", size=9)
    ALT_FILL = PatternFill("solid", fgColor="EEF3F8")
    CENTER  = Alignment(horizontal="center", vertical="center", wrap_text=True)
    LEFT    = Alignment(horizontal="left",   vertical="center", wrap_text=True)
    THIN    = Side(style="thin", color="C8D9E8")
    BORDER  = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

    def _h(cell, val):
        cell.value = val
        cell.font  = H_FONT;  cell.fill = H_FILL
        cell.alignment = CENTER; cell.border = BORDER

    def _sh(cell, val):
        cell.value = val
        cell.font  = SH_FONT; cell.fill = SH_FILL
        cell.alignment = CENTER; cell.border = BORDER

    def _d(cell, val, align=CENTER, alt=False):
        cell.value = val
        cell.alignment = align; cell.border = BORDER
        if alt:
            cell.fill = ALT_FILL

    # ── SUMMARY sheet ─────────────────────────────────────────────────────────
    ws = wb.active
    ws.title = "Summary"

    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=5)
    tc = ws.cell(row=1, column=1)
    tc.value = f"Grading Report — {data['exam_name']}"
    tc.font  = Font(bold=True, size=13, color="1A3550")
    tc.alignment = CENTER
    ws.row_dimensions[1].height = 28

    for ci, h in enumerate(
        ["Student ID", "Student Name", "Total Score", "Max Score", "Percentage (%)"],
        start=1,
    ):
        _h(ws.cell(row=2, column=ci), h)
    ws.row_dimensions[2].height = 24

    for ri, s in enumerate(data["students"], start=3):
        alt = ri % 2 == 0
        _d(ws.cell(ri, 1), s["student_id"],   LEFT,   alt)
        _d(ws.cell(ri, 2), s["student_name"], LEFT,   alt)
        _d(ws.cell(ri, 3), s["total_score"],  CENTER, alt)
        _d(ws.cell(ri, 4), s["max_total"],    CENTER, alt)
        _d(ws.cell(ri, 5), s["percentage"],   CENTER, alt)

    ws.column_dimensions["A"].width = 16
    ws.column_dimensions["B"].width = 28
    ws.column_dimensions["C"].width = 14
    ws.column_dimensions["D"].width = 14
    ws.column_dimensions["E"].width = 18
    ws.freeze_panes = "A3"

    # ── QUESTION DETAIL sheet ─────────────────────────────────────────────────
    wd = wb.create_sheet("Question Detail")

    base_headers = [
        "Student ID", "Student Name", "Question ID", "Question Text",
        "Final Score", "Max Score", "Basis Used",
        "Reason", "Matched Points", "Missing Points",
    ]
    LEFT_COLS = {1, 2, 4, 8, 9, 10}  # 1-indexed column numbers that use LEFT align

    if data["is_consensus"]:
        n_ex = _max_examiners(data, "consensus")
        ex_headers: list[str] = []
        for i in range(1, n_ex + 1):
            ex_headers += [
                f"Ex.{i} Init Score", f"Ex.{i} Init Reason",
                f"Ex.{i} Init Matched", f"Ex.{i} Init Missing",
                f"Ex.{i} Final Score", f"Ex.{i} Changed", f"Ex.{i} Revision",
            ]
        all_headers = base_headers + ex_headers

        for ci, h in enumerate(all_headers, start=1):
            _h(wd.cell(1, ci), h)

        ri = 2
        for s in data["students"]:
            for qid in data["columns"]:
                q  = s["questions"].get(qid, {})
                cd = q.get("consensus_detail", {})
                init_by = {r["examiner_label"]: r for r in cd.get("initial", [])}
                rev_by  = {r["examiner_label"]: r for r in cd.get("revised", [])}
                labels  = sorted(set(list(init_by) + list(rev_by)))
                while len(labels) < n_ex:
                    labels.append(f"Examiner {len(labels) + 1}")

                alt  = ri % 2 == 0
                vals = [
                    s["student_id"], s["student_name"], qid,
                    q.get("question_text", ""),
                    q.get("final_score", 0), q.get("max_score", 0),
                    q.get("basis_used", ""), q.get("reason", ""),
                    _fmt_list(q.get("matched_points", [])),
                    _fmt_list(q.get("missing_points", [])),
                ]
                for lbl in labels:
                    ir = init_by.get(lbl, {})
                    rr = rev_by.get(lbl, {})
                    vals += [
                        ir.get("score", ""),
                        ir.get("reason", ""),
                        _fmt_list(ir.get("matched_points", [])),
                        _fmt_list(ir.get("missing_points", [])),
                        rr.get("score", ""),
                        ("Yes" if rr.get("score_changed") else "No") if rr else "",
                        rr.get("revision_reason", ""),
                    ]
                for ci, v in enumerate(vals, start=1):
                    _d(wd.cell(ri, ci), v, LEFT if ci in LEFT_COLS else CENTER, alt)
                ri += 1

    else:
        n_ex = _max_examiners(data, "individual")
        ex_headers = []
        for i in range(1, n_ex + 1):
            ex_headers += [
                f"Ex.{i} Score", f"Ex.{i} Reason",
                f"Ex.{i} Matched", f"Ex.{i} Missing",
            ]
        all_headers = base_headers + ex_headers

        for ci, h in enumerate(all_headers, start=1):
            _h(wd.cell(1, ci), h)

        ri = 2
        for s in data["students"]:
            for qid in data["columns"]:
                q   = s["questions"].get(qid, {})
                ind = q.get("individual_detail", [])
                alt = ri % 2 == 0
                vals = [
                    s["student_id"], s["student_name"], qid,
                    q.get("question_text", ""),
                    q.get("final_score", 0), q.get("max_score", 0),
                    q.get("basis_used", ""), q.get("reason", ""),
                    _fmt_list(q.get("matched_points", [])),
                    _fmt_list(q.get("missing_points", [])),
                ]
                for i in range(n_ex):
                    d = ind[i] if i < len(ind) else {}
                    vals += [
                        d.get("score", ""),
                        d.get("reason", ""),
                        _fmt_list(d.get("matched_points", [])),
                        _fmt_list(d.get("missing_points", [])),
                    ]
                for ci, v in enumerate(vals, start=1):
                    _d(wd.cell(ri, ci), v, LEFT if ci in LEFT_COLS else CENTER, alt)
                ri += 1

    # Column widths for detail sheet
    wd.column_dimensions["A"].width = 16
    wd.column_dimensions["B"].width = 22
    wd.column_dimensions["C"].width = 10
    wd.column_dimensions["D"].width = 42
    for i in range(5, len(all_headers) + 1):
        wd.column_dimensions[get_column_letter(i)].width = 20
    wd.freeze_panes = "A2"

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


# ══════════════════════════════════════════════════════════════════════════════
#  PDF
# ══════════════════════════════════════════════════════════════════════════════

def _safe_pdf_text(value) -> str:
    """
    Sanitise a value for fpdf2 Helvetica (Latin-1) output.

    1. Converts to str, strips newlines.
    2. Replaces Unicode dash variants with ASCII hyphen.
    3. Replaces the ellipsis character with three dots.
    4. Encodes to latin-1 with 'replace' so any remaining non-Latin-1
       characters (e.g. Arabic) become '?' instead of crashing.
    """
    if value is None:
        return ""
    text = str(value).replace("\n", " ").replace("\r", " ")
    text = (
        text
        .replace("\u2014", "-")   # em dash  —
        .replace("\u2013", "-")   # en dash  –
        .replace("\u2026", "...")  # ellipsis …
        .replace("\u2018", "'")   # left single quote
        .replace("\u2019", "'")   # right single quote
        .replace("\u201c", '"')   # left double quote
        .replace("\u201d", '"')   # right double quote
    )
    return text.encode("latin-1", "replace").decode("latin-1")


def generate_pdf(
    exam_id: int,
    db: Session,
    job_id: Optional[int] = None,
) -> bytes:
    from fpdf import FPDF

    job  = _resolve_job(job_id, exam_id, db)
    data = _build_report_data(job, exam_id, db)

    L_MARGIN = 15
    R_MARGIN = 15
    PAGE_W   = 210 - L_MARGIN - R_MARGIN  # A4 portrait content width

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=14)
    pdf.set_margins(L_MARGIN, 15, R_MARGIN)

    def _clip(text, max_len: int = 250) -> str:
        """Clip to max_len chars then sanitise for Latin-1 output."""
        s = _safe_pdf_text(text)
        return (s[:max_len] + "...") if len(s) > max_len else s

    def _field(label: str, value: str, indent: float = 5.0) -> None:
        if not value:
            return
        pdf.set_x(pdf.l_margin + indent)
        pdf.set_text_color(40, 55, 75)
        pdf.set_font("Helvetica", "", 8)
        full = _safe_pdf_text(f"{label}: {value}")
        pdf.multi_cell(PAGE_W - indent, 4.5, full, border=0)

    # ── TITLE / SUMMARY page ──────────────────────────────────────────────────
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 13)
    pdf.set_text_color(27, 53, 80)
    pdf.cell(0, 9, "Grading Report", ln=True, align="C")
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(60, 80, 100)
    pdf.cell(0, 6, _safe_pdf_text(data["exam_name"]), ln=True, align="C")
    pdf.ln(5)

    # Summary table header
    col_w = [30, 70, 25, 22, 22]
    headers = ["Student ID", "Student Name", "Total", "Max", "Pct (%)"]

    def _summary_header():
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_fill_color(43, 74, 111)
        pdf.set_text_color(255, 255, 255)
        for w, h in zip(col_w, headers):
            pdf.cell(w, 7, h, border=1, fill=True, align="C")
        pdf.ln()

    _summary_header()

    for idx, s in enumerate(data["students"]):
        if pdf.get_y() > pdf.h - 20:
            pdf.add_page()
            _summary_header()

        fill_rgb = (238, 243, 248) if idx % 2 == 0 else (255, 255, 255)
        pdf.set_fill_color(*fill_rgb)
        pdf.set_text_color(30, 40, 55)
        pdf.set_font("Helvetica", "", 7)

        pdf.cell(col_w[0], 6, _clip(s["student_id"],  26), border=1, fill=True, align="L")
        pdf.cell(col_w[1], 6, _clip(s["student_name"], 42), border=1, fill=True, align="L")
        pdf.cell(col_w[2], 6, str(s["total_score"]),  border=1, fill=True, align="C")
        pdf.cell(col_w[3], 6, str(s["max_total"]),    border=1, fill=True, align="C")
        pdf.cell(col_w[4], 6, f"{s['percentage']}%",  border=1, fill=True, align="C")
        pdf.ln()

    # ── PER-STUDENT DETAIL PAGES ───────────────────────────────────────────────
    for s in data["students"]:
        pdf.add_page()

        # Student banner — ASCII hyphen instead of em-dash
        pdf.set_fill_color(43, 74, 111)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 10)
        banner = (
            f"  {s['student_id']}  -  {s['student_name']}   "
            f"({s['total_score']} / {s['max_total']} = {s['percentage']}%)"
        )
        pdf.cell(0, 9, _clip(banner, 120), border=0, fill=True, ln=True)
        pdf.ln(2)

        for qid in data["columns"]:
            q = s["questions"].get(qid, {})

            # Question header bar — ASCII hyphen instead of em-dash
            pdf.set_fill_color(74, 123, 175)
            pdf.set_text_color(255, 255, 255)
            pdf.set_font("Helvetica", "B", 9)
            score_str = f"{q.get('final_score', 0)} / {q.get('max_score', 0)}"
            pdf.cell(0, 6, _safe_pdf_text(f"  {qid}  -  {score_str}"), border=0, fill=True, ln=True)

            # Question text
            qtxt = q.get("question_text", "")
            if qtxt:
                pdf.set_fill_color(240, 246, 252)
                pdf.set_text_color(30, 50, 70)
                pdf.set_font("Helvetica", "I", 8)
                pdf.set_x(pdf.l_margin)
                pdf.multi_cell(0, 4.5, _safe_pdf_text(qtxt), border=0, fill=True)

            # Core fields
            _field("Basis",   q.get("basis_used", ""))
            _field("Reason",  _safe_pdf_text(q.get("reason", "")))
            _field("Matched", _safe_pdf_text(_fmt_list(q.get("matched_points", []))))
            _field("Missing", _safe_pdf_text(_fmt_list(q.get("missing_points", []))))

            if data["is_consensus"]:
                cd       = q.get("consensus_detail", {})
                init_lst = cd.get("initial", [])
                rev_by   = {r["examiner_label"]: r for r in cd.get("revised", [])}

                if init_lst:
                    pdf.set_x(pdf.l_margin + 5)
                    pdf.set_font("Helvetica", "B", 8)
                    pdf.set_text_color(60, 90, 130)
                    pdf.cell(0, 5, "Examiner Details:", ln=True)

                for r in init_lst:
                    lbl = r["examiner_label"]
                    rr  = rev_by.get(lbl, {})
                    suffix = ""
                    if rr:
                        ch = "Yes" if rr.get("score_changed") else "No"
                        suffix = f" | Final: {rr.get('score','')} (changed: {ch})"
                        if rr.get("revision_reason"):
                            suffix += f" - {_safe_pdf_text(rr['revision_reason'])}"
                    _field(
                        lbl,
                        f"Initial: {r['score']} - {_safe_pdf_text(r.get('reason',''))}{suffix}",
                        indent=10.0,
                    )
            else:
                ind = q.get("individual_detail", [])
                if len(ind) > 1:
                    pdf.set_x(pdf.l_margin + 5)
                    pdf.set_font("Helvetica", "B", 8)
                    pdf.set_text_color(60, 90, 130)
                    pdf.cell(0, 5, "Examiner Scores:", ln=True)
                    for r in ind:
                        _field(
                            r.get("examiner_label", "Examiner"),
                            f"{r['score']} - {_safe_pdf_text(r.get('reason',''))}",
                            indent=10.0,
                        )

            pdf.ln(3)

    return bytes(pdf.output())
