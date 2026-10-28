"""
Reviewed Export Router -- /reviewed-export
=========================================
Exports reviewed student exam papers with per-question AI reasoning,
scores (original vs reviewed), and grade summaries.

Endpoint map (note: exam/ routes declared BEFORE /{result_id}/ to avoid
FastAPI capturing the literal "exam" as a result_id integer):

GET /reviewed-export/exam/{exam_id}/export/csv     -- all-exam CSV
GET /reviewed-export/exam/{exam_id}/export/excel   -- all-exam Excel (.xlsx)
GET /reviewed-export/exam/{exam_id}/export/pdf     -- all-exam PDF

GET /reviewed-export/{result_id}/export/csv        -- single-student CSV
GET /reviewed-export/{result_id}/export/excel      -- single-student Excel
GET /reviewed-export/{result_id}/export/pdf        -- single-student PDF
"""

from __future__ import annotations

import csv
import io
import os
import re
import unicodedata
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from database import get_db
from deps import get_current_user
from models import Exam, ExamResult, StudentQuestionScore, User
from permissions import require_exam_access

router = APIRouter(prefix="/reviewed-export", tags=["Reviewed Export"])


# =============================================================================
#  Data helpers
# =============================================================================

def _get_exam_or_404(exam_id: int, db: Session) -> Exam:
    exam = db.get(Exam, exam_id)
    if exam is None:
        raise HTTPException(status_code=404, detail=f"Exam {exam_id} not found.")
    return exam


def _get_result_or_404(result_id: int, db: Session) -> ExamResult:
    result = db.get(ExamResult, result_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Result {result_id} not found.")
    return result


def _build_student_data(
    result: ExamResult,
    exam: Exam,
    scores: List[StudentQuestionScore],
) -> dict:
    """Assemble all export data for a single student result."""
    date_str = str(exam.date) if exam.date else ""
    semester_map = {
        "first":  "First Semester",
        "second": "Second Semester",
        "summer": "Summer Semester",
    }
    semester_str = semester_map.get(
        exam.semester.value if exam.semester else "", ""
    ) if exam.semester else ""

    questions = []
    for sq in sorted(scores, key=lambda s: s.question_number):
        orig     = float(sq.original_score) if sq.original_score is not None else float(sq.score)
        reviewed = float(sq.score)
        questions.append({
            "question_number": sq.question_number,
            "question_text":   sq.question_text or f"Question {sq.question_number}",
            "student_answer":  sq.student_answer or "",
            "ai_reasoning":    sq.ai_reasoning or "",
            "original_score":  orig,
            "reviewed_score":  reviewed,
            "max_score":       float(sq.max_score),
            "manually_edited": reviewed != orig,
        })

    return {
        "exam_id":         exam.id,
        "exam_name":       exam.name or f"Exam {exam.id}",
        "exam_type":       exam.type.value if exam.type else "",
        "exam_section":    exam.section or "",
        "exam_date":       date_str,
        "exam_semester":   semester_str,
        "total_marks":     exam.total_marks or 0,
        "result_id":       result.id,
        "student_id":      result.student_id or "",
        "student_name":    result.student_name or "",
        "total_score":     float(result.total_score or 0),
        "grade":           result.grade or "",
        "status":          result.status.value if result.status else "",
        "manually_edited": bool(result.manually_edited),
        "questions":       questions,
        "questions_max":   sum(q["max_score"] for q in questions),
    }


def _load_all_students(exam_id: int, db: Session, exam: Exam) -> List[dict]:
    """Load and assemble export data for all students in an exam."""
    results: List[ExamResult] = (
        db.query(ExamResult)
        .filter(ExamResult.exam_id == exam_id)
        .order_by(ExamResult.student_id)
        .all()
    )
    if not results:
        return []

    out = []
    for result in results:
        sc = (
            db.query(StudentQuestionScore)
            .filter(StudentQuestionScore.result_id == result.id)
            .order_by(StudentQuestionScore.question_number)
            .all()
        )
        out.append(_build_student_data(result, exam, sc))
    return out


# =============================================================================
#  CSV builder
# =============================================================================

def _build_csv(students: List[dict]) -> bytes:
    """UTF-8-BOM CSV: one row per question per student."""
    buf = io.StringIO()
    writer = csv.writer(buf)

    writer.writerow([
        "Student ID", "Student Name", "Total Score", "Max Score",
        "Grade", "Status", "Manually Edited",
        "Q#", "Question", "Student Answer", "AI Reasoning",
        "Original Score", "Reviewed Score", "Q Max Score", "Score Edited",
    ])

    for s in students:
        if not s["questions"]:
            writer.writerow([
                s["student_id"], s["student_name"], s["total_score"],
                s["total_marks"], s["grade"], s["status"],
                "Yes" if s["manually_edited"] else "No",
                "", "", "", "", "", "", "", "",
            ])
            continue
        for i, q in enumerate(s["questions"]):
            writer.writerow([
                s["student_id"]                        if i == 0 else "",
                s["student_name"]                      if i == 0 else "",
                s["total_score"]                       if i == 0 else "",
                s["total_marks"]                       if i == 0 else "",
                s["grade"]                             if i == 0 else "",
                s["status"]                            if i == 0 else "",
                "Yes" if s["manually_edited"] else "No" if i == 0 else "",
                q["question_number"],
                q["question_text"],
                q["student_answer"],
                q["ai_reasoning"],
                q["original_score"],
                q["reviewed_score"],
                q["max_score"],
                "Yes" if q["manually_edited"] else "No",
            ])

    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")


# =============================================================================
#  Excel builder
# =============================================================================

def _build_excel(students: List[dict], exam_name: str) -> bytes:
    """Two-sheet .xlsx: Summary + Grading Detail."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="openpyxl is not installed on the server.",
        )

    wb = Workbook()
    NAVY       = PatternFill("solid", fgColor="1E3A5F")
    WHITE_BOLD = Font(bold=True, color="FFFFFF", name="Calibri", size=11)
    CENTER     = Alignment(horizontal="center", vertical="center")
    TOP_WRAP   = Alignment(wrap_text=True, vertical="top")

    ws1 = wb.active
    ws1.title = "Summary"
    for col, h in enumerate(
        ["Student ID", "Student Name", "Total Score", "Max Score",
         "Grade", "Status", "Manually Edited"], 1
    ):
        c = ws1.cell(row=1, column=col, value=h)
        c.font = WHITE_BOLD; c.fill = NAVY; c.alignment = CENTER

    for ri, s in enumerate(students, 2):
        ws1.cell(row=ri, column=1, value=s["student_id"])
        ws1.cell(row=ri, column=2, value=s["student_name"])
        ws1.cell(row=ri, column=3, value=s["total_score"])
        ws1.cell(row=ri, column=4, value=s["total_marks"])
        ws1.cell(row=ri, column=5, value=s["grade"])
        ws1.cell(row=ri, column=6, value=s["status"])
        ws1.cell(row=ri, column=7, value="Yes" if s["manually_edited"] else "No")

    for col, w in enumerate([16, 28, 14, 12, 10, 16, 18], 1):
        ws1.column_dimensions[get_column_letter(col)].width = w

    ws2 = wb.create_sheet("Grading Detail")
    for col, h in enumerate(
        ["Student ID", "Student Name", "Q#", "Question", "Student Answer",
         "AI Reasoning", "Original Score", "Reviewed Score", "Q Max Score", "Score Edited"], 1
    ):
        c = ws2.cell(row=1, column=col, value=h)
        c.font = WHITE_BOLD; c.fill = NAVY; c.alignment = CENTER

    ri = 2
    for s in students:
        for qi, q in enumerate(s["questions"]):
            ws2.cell(row=ri, column=1,  value=s["student_id"]   if qi == 0 else "")
            ws2.cell(row=ri, column=2,  value=s["student_name"] if qi == 0 else "")
            ws2.cell(row=ri, column=3,  value=q["question_number"])
            ws2.cell(row=ri, column=4,  value=q["question_text"])
            ws2.cell(row=ri, column=5,  value=q["student_answer"])
            ws2.cell(row=ri, column=6,  value=q["ai_reasoning"])
            ws2.cell(row=ri, column=7,  value=q["original_score"])
            ws2.cell(row=ri, column=8,  value=q["reviewed_score"])
            ws2.cell(row=ri, column=9,  value=q["max_score"])
            ws2.cell(row=ri, column=10, value="Yes" if q["manually_edited"] else "No")
            ri += 1

    for col, w in enumerate([16, 28, 5, 40, 50, 60, 14, 14, 12, 12], 1):
        ws2.column_dimensions[get_column_letter(col)].width = w
    for row in ws2.iter_rows(min_row=2, min_col=4, max_col=6):
        for cell in row:
            cell.alignment = TOP_WRAP

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# =============================================================================
#  PDF text helpers
# =============================================================================

# Common Unicode characters with clean ASCII/Latin-1 equivalents
_UNICODE_MAP: dict[str, str] = {
    "\u2014": "--",    # em-dash
    "\u2013": "-",     # en-dash
    "\u2012": "-",     # figure dash
    "\u2010": "-",     # hyphen
    "\u2011": "-",     # non-breaking hyphen
    "\u2018": "'",     # left single quote
    "\u2019": "'",     # right single quote
    "\u201a": ",",     # single low-9 quote
    "\u201b": "'",     # single high-reversed-9 quote
    "\u201c": '"',     # left double quote
    "\u201d": '"',     # right double quote
    "\u201e": '"',     # double low-9 quote
    "\u201f": '"',     # double high-reversed-9 quote
    "\u2026": "...",   # ellipsis
    "\u00a0": " ",     # non-breaking space
    "\u200b": "",      # zero-width space
    "\u200c": "",      # zero-width non-joiner
    "\u200d": "",      # zero-width joiner
    "\u2022": "-",     # bullet
    "\u2023": "-",     # triangular bullet
    "\u25cf": "-",     # black circle
    "\u25b6": ">",     # black right-pointing triangle
    "\u00b7": ".",     # middle dot
    "\u2212": "-",     # minus sign
    "\u00d7": "x",     # multiplication sign
    "\u00f7": "/",     # division sign
    "\u2248": "~",     # almost equal
    "\u2260": "!=",    # not equal
    "\u2264": "<=",    # less-than or equal
    "\u2265": ">=",    # greater-than or equal
    "\u221a": "sqrt",  # square root
    "\u221e": "inf",   # infinity
    "\u2211": "sum",   # summation
    "\u222b": "int",   # integral
    "\u03b1": "alpha", "\u03b2": "beta",  "\u03b3": "gamma",
    "\u03b4": "delta", "\u03b5": "eps",   "\u03b8": "theta",
    "\u03bb": "lambda","\u03bc": "mu",    "\u03c0": "pi",
    "\u03c3": "sigma", "\u03c4": "tau",   "\u03c6": "phi",
    "\u0394": "Delta", "\u03a3": "Sigma", "\u03a9": "Omega",
    "\u00b0": "deg",   # degree sign
    "\u00b2": "^2",    # superscript 2
    "\u00b3": "^3",    # superscript 3
    "\u00b9": "^1",    # superscript 1
    "\u2070": "^0",    # superscript 0
    "\u2032": "'",     # prime
    "\u2033": "''",    # double prime
}


def _pdf_text(text: str) -> str:
    """
    Convert arbitrary Unicode text to Latin-1 safe string for fpdf core fonts.

    Strategy:
      1. Apply known character substitutions (em-dash -> --, quotes, Greek, etc.)
      2. NFKD normalization to strip diacritics from accented Latin chars
      3. Any remaining non-Latin-1 char (Arabic, CJK, etc.) is dropped with a
         space as separator so word boundaries are preserved.

    Never raises -- always returns a printable string.
    """
    if not text:
        return ""

    # Step 1: known replacements
    for src, dst in _UNICODE_MAP.items():
        if src in text:
            text = text.replace(src, dst)

    # Step 2: NFKD + ASCII fallback per character
    result: list[str] = []
    for ch in text:
        cp = ord(ch)
        if cp < 256:
            result.append(ch)
            continue
        # Try NFKD decomposition (e.g. e + combining accent -> e)
        norm = unicodedata.normalize("NFKD", ch)
        ascii_ch = norm.encode("ascii", "ignore").decode("ascii")
        if ascii_ch:
            result.append(ascii_ch)
        else:
            # Completely unrepresentable (Arabic, CJK, etc.) -- insert a space
            # so we don't run adjacent transliterated words together
            if not result or result[-1] != " ":
                result.append(" ")

    return "".join(result).strip()


def _find_unicode_ttf() -> Optional[str]:
    """
    Return the path to a system TTF font that supports Unicode, or None.
    Checked in priority order: macOS first, then Linux, then Windows.
    """
    candidates = [
        # macOS
        "/Library/Fonts/Arial Unicode.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        # Ubuntu / Debian
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
        "/usr/share/fonts/truetype/unifont/unifont.ttf",
        # Generic Linux
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        # Windows
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\calibri.ttf",
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


# =============================================================================
#  PDF builder
# =============================================================================

def _build_pdf(students: List[dict], exam_name: str) -> bytes:
    """
    Professional academic PDF: one page per student, per-question breakdown.

    Font strategy
    -------------
    1. Probe common system paths for a Unicode-capable TTF (Arial Unicode,
       DejaVu, Liberation, etc.).  If found, use it so Arabic names/answers
       render correctly.
    2. If no Unicode font is found, fall back to Helvetica (a core PDF font)
       and sanitise all text with _pdf_text() so that no
       FPDFUnicodeEncodingException can ever be raised.

    Both paths produce identical visual layouts.
    """
    try:
        from fpdf import FPDF
    except ImportError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="fpdf2 is not installed on the server.",
        )

    unicode_font_path = _find_unicode_ttf()
    use_unicode       = unicode_font_path is not None

    # Pre-sanitise the strings that are always in our template (em-dashes etc.)
    safe_exam_name  = _pdf_text(exam_name)
    header_title    = f"Taqdeer -- {safe_exam_name}"
    footer_static   = "Taqdeer Academic Grading Platform -- Instructor Use Only"

    class PDF(FPDF):
        # ------------------------------------------------------------------
        # Internal helper: set font regardless of whether we use TTF or core
        # ------------------------------------------------------------------
        def F(self, style: str = "", size: int = 10) -> None:
            if use_unicode:
                # Map combined styles to individual bold/italic variants we registered
                b = "B" if "B" in style else ""
                i = "I" if "I" in style else ""
                # fpdf2: bold-italic would need a separate font file; use bold for BI
                variant = b or i or ""
                self.set_font("UF", variant, size)
            else:
                self.set_font("Helvetica", style, size)

        def T(self, text: str) -> str:
            """Return text as-is with Unicode font; sanitise for core fonts."""
            return text if use_unicode else _pdf_text(text)

        def header(self):
            self.set_fill_color(30, 58, 95)
            self.rect(0, 0, 210, 18, "F")
            self.set_y(3)
            self.F("B", 13)
            self.set_text_color(255, 255, 255)
            self.cell(0, 12, header_title, align="L", border=0)
            self.set_text_color(0, 0, 0)
            self.ln(16)

        def footer(self):
            self.set_y(-13)
            self.F("", 8)
            self.set_text_color(150, 150, 150)
            self.cell(0, 10, f"Page {self.page_no()} | {footer_static}", align="C")
            self.set_text_color(0, 0, 0)

    pdf = PDF()
    pdf.set_auto_page_break(auto=True, margin=18)

    # Register Unicode TTF (all three variants point to the same file; fpdf2
    # will synthesise bold/italic if separate files are not available)
    if use_unicode:
        try:
            pdf.add_font("UF", "",  unicode_font_path)
            pdf.add_font("UF", "B", unicode_font_path)
            pdf.add_font("UF", "I", unicode_font_path)
        except Exception:
            # Font registration failed -- silently fall back to core Helvetica
            use_unicode = False

    # ------------------------------------------------------------------
    # Per-student pages
    # ------------------------------------------------------------------
    for s in students:
        pdf.add_page()

        student_name = pdf.T(s["student_name"])
        pct          = (s["total_score"] / s["total_marks"] * 100) if s["total_marks"] else 0.0
        summary_line = (
            f'ID: {s["student_id"]}   |   '
            f'Score: {s["total_score"]} / {s["total_marks"]} ({pct:.1f}%)   |   '
            f'Grade: {s["grade"]}   |   '
            f'Status: {s["status"].replace("_", " ").title()}'
        )

        # Student summary box
        box_y = pdf.get_y()
        pdf.set_fill_color(240, 245, 252)
        pdf.set_draw_color(180, 200, 220)
        pdf.rect(10, box_y, 190, 24, "FD")
        pdf.set_x(14)
        pdf.F("B", 12)
        pdf.cell(0, 8, student_name)
        pdf.ln(8)
        pdf.set_x(14)
        pdf.F("", 10)
        pdf.cell(0, 8, pdf.T(summary_line))
        pdf.ln(12)

        # ------------------------------------------------------------------
        # Per-question blocks
        # ------------------------------------------------------------------
        for q in s["questions"]:
            pct_q       = (q["reviewed_score"] / q["max_score"] * 100) if q["max_score"] else 0.0
            score_color = (5, 150, 105) if pct_q >= 60 else (220, 38, 38)
            q_header    = pdf.T(f'Q{q["question_number"]}  {q["question_text"]}')

            # Question header band (background + text)
            pdf.set_fill_color(208, 223, 235)
            pdf.F("B", 10)
            pdf.set_x(10)
            before_y = pdf.get_y()
            pdf.multi_cell(150, 6, q_header, fill=True, border=0)
            after_y = pdf.get_y()

            # Score badge -- top-right, same row as question header
            pdf.set_xy(162, before_y)
            pdf.set_text_color(*score_color)
            pdf.F("B", 10)
            pdf.cell(38, 6, f'{q["reviewed_score"]:.0f} / {q["max_score"]:.0f}', align="R")
            pdf.set_text_color(0, 0, 0)
            pdf.set_y(after_y)

            if q["manually_edited"]:
                pdf.F("I", 8)
                pdf.set_text_color(100, 100, 100)
                pdf.set_x(12)
                pdf.cell(0, 5, f'  * Edited -- original score: {q["original_score"]:.0f}')
                pdf.set_text_color(0, 0, 0)
                pdf.ln(1)

            # Student answer
            pdf.F("B", 8)
            pdf.set_x(12)
            pdf.cell(0, 5, "Student Answer:")
            pdf.ln(4)
            pdf.F("", 8)
            pdf.set_x(14)
            answer_text = pdf.T(q["student_answer"]) if q["student_answer"] else "(no answer recorded)"
            pdf.multi_cell(186, 4.5, answer_text)
            pdf.ln(1)

            # AI Reasoning
            pdf.F("B", 8)
            pdf.set_x(12)
            pdf.cell(0, 5, "AI Reasoning:")
            pdf.ln(4)
            pdf.F("I", 8)
            pdf.set_text_color(80, 80, 80)
            pdf.set_x(14)
            reasoning_text = pdf.T(q["ai_reasoning"]) if q["ai_reasoning"] else "(no reasoning available)"
            pdf.multi_cell(186, 4.5, reasoning_text)
            pdf.set_text_color(0, 0, 0)
            pdf.ln(4)

        # Total footer bar
        pdf.set_fill_color(30, 58, 95)
        pdf.set_text_color(255, 255, 255)
        pdf.F("B", 11)
        pdf.set_x(10)
        pdf.cell(
            190, 10,
            f'Total Score: {s["total_score"]} / {s["total_marks"]}   Grade: {s["grade"]}',
            fill=True, align="C",
        )
        pdf.set_text_color(0, 0, 0)

    return bytes(pdf.output())


# =============================================================================
#  Filename sanitiser
# =============================================================================

def _safe_filename(name: str) -> str:
    return re.sub(r"[^\w\-]", "_", name)[:60]


# =============================================================================
#  Per-EXAM routes  (MUST be declared before /{result_id}/ routes)
# =============================================================================

@router.get("/exam/{exam_id}/export/csv")
async def export_exam_csv(
    exam_id:      int,
    db:           Session = Depends(get_db),
    current_user: User    = Depends(get_current_user),
):
    exam     = _get_exam_or_404(exam_id, db)
    require_exam_access(exam, current_user)
    students = _load_all_students(exam_id, db, exam)
    if not students:
        raise HTTPException(status_code=404, detail="No graded results found for this exam.")
    content  = _build_csv(students)
    filename = _safe_filename(exam.name or f"exam_{exam_id}") + "_reviewed.csv"
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/exam/{exam_id}/export/excel")
async def export_exam_excel(
    exam_id:      int,
    db:           Session = Depends(get_db),
    current_user: User    = Depends(get_current_user),
):
    exam     = _get_exam_or_404(exam_id, db)
    require_exam_access(exam, current_user)
    students = _load_all_students(exam_id, db, exam)
    if not students:
        raise HTTPException(status_code=404, detail="No graded results found for this exam.")
    content  = _build_excel(students, exam.name or f"Exam {exam_id}")
    filename = _safe_filename(exam.name or f"exam_{exam_id}") + "_reviewed.xlsx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/exam/{exam_id}/export/pdf")
async def export_exam_pdf(
    exam_id:      int,
    db:           Session = Depends(get_db),
    current_user: User    = Depends(get_current_user),
):
    exam     = _get_exam_or_404(exam_id, db)
    require_exam_access(exam, current_user)
    students = _load_all_students(exam_id, db, exam)
    if not students:
        raise HTTPException(status_code=404, detail="No graded results found for this exam.")
    content  = _build_pdf(students, exam.name or f"Exam {exam_id}")
    filename = _safe_filename(exam.name or f"exam_{exam_id}") + "_reviewed.pdf"
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# =============================================================================
#  Per-STUDENT (result_id) routes
# =============================================================================

@router.get("/{result_id}/export/csv")
async def export_result_csv(
    result_id:    int,
    db:           Session = Depends(get_db),
    current_user: User    = Depends(get_current_user),
):
    result  = _get_result_or_404(result_id, db)
    exam    = _get_exam_or_404(result.exam_id, db)
    require_exam_access(exam, current_user)
    scores  = (
        db.query(StudentQuestionScore)
        .filter(StudentQuestionScore.result_id == result_id)
        .order_by(StudentQuestionScore.question_number)
        .all()
    )
    student  = _build_student_data(result, exam, scores)
    content  = _build_csv([student])
    filename = _safe_filename(result.student_id or f"result_{result_id}") + "_reviewed.csv"
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{result_id}/export/excel")
async def export_result_excel(
    result_id:    int,
    db:           Session = Depends(get_db),
    current_user: User    = Depends(get_current_user),
):
    result  = _get_result_or_404(result_id, db)
    exam    = _get_exam_or_404(result.exam_id, db)
    require_exam_access(exam, current_user)
    scores  = (
        db.query(StudentQuestionScore)
        .filter(StudentQuestionScore.result_id == result_id)
        .order_by(StudentQuestionScore.question_number)
        .all()
    )
    student  = _build_student_data(result, exam, scores)
    content  = _build_excel([student], exam.name or f"Exam {result.exam_id}")
    filename = _safe_filename(result.student_id or f"result_{result_id}") + "_reviewed.xlsx"
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{result_id}/export/pdf")
async def export_result_pdf(
    result_id:    int,
    db:           Session = Depends(get_db),
    current_user: User    = Depends(get_current_user),
):
    result  = _get_result_or_404(result_id, db)
    exam    = _get_exam_or_404(result.exam_id, db)
    require_exam_access(exam, current_user)
    scores  = (
        db.query(StudentQuestionScore)
        .filter(StudentQuestionScore.result_id == result_id)
        .order_by(StudentQuestionScore.question_number)
        .all()
    )
    student  = _build_student_data(result, exam, scores)
    content  = _build_pdf([student], exam.name or f"Exam {result.exam_id}")
    filename = _safe_filename(result.student_id or f"result_{result_id}") + "_reviewed.pdf"
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
