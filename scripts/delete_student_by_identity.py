"""
delete_student_by_identity.py — dev/admin cleanup script
=========================================================
Finds and deletes ALL database records (and physical files) for one student
paper, identified by student number + exam name + section.  All IDs are
resolved automatically — you do not need to know them.

Usage
-----
  # Dry-run (prints everything that would be deleted, touches nothing):
  python scripts/delete_student_by_identity.py \\
      --student-number 1846816 --exam "Final" --section "EA" --dry-run

  # Live delete (prompts for confirmation, then commits one transaction):
  python scripts/delete_student_by_identity.py \\
      --student-number 1846816 --exam "Final" --section "EA"

Run from the project root (taqdeer/), not from inside backend/.

What is deleted
---------------
  DB rows (child-first order):
    1. student_question_scores   — per-question scores for this student
    2. exam_results              — the student's graded result record
    3. grading_final_scores      — final consolidated scores per question/job
    4. model_grading_results     — raw per-model AI grading outputs
    5. student_answer_items      — extracted answer text per question
    6. student_extractions       — the extraction record itself
    7. answer_files              — the uploaded PDF record

  Physical files (only if they exist on disk):
    8. answer PDF                — backend/{answer_file.file_path}
    9. extraction JSON           — backend/{extraction.output_json_path}

What is NOT deleted
-------------------
  - Exam, Rubric, ExamModelAnswer (answer key)
  - Any other student's records
  - grading_jobs            (shared across all students in the exam)
  - grading_reports         (shared across the exam)
  - exam_gap_analysis       (shared across the exam)
  - activity_log / notifications (audit trail; not worth cleaning)
"""

import argparse
import os
import sys
import unicodedata
from typing import Optional

# ── Make backend importable when running from project root ──────────────────
_script_dir  = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.abspath(os.path.join(_script_dir, ".."))
_backend_dir  = os.path.join(_project_root, "backend")
sys.path.insert(0, _backend_dir)

from dotenv import load_dotenv
from pathlib import Path

load_dotenv(dotenv_path=Path(_backend_dir) / ".env", override=True)

from sqlalchemy import func
from database import SessionLocal
from models import (
    AnswerFile,
    Exam,
    ExamResult,
    GradingFinalScore,
    ModelGradingResult,
    StudentAnswerItem,
    StudentExtraction,
    StudentQuestionScore,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _strip_diacritics(text: str) -> str:
    """Remove Unicode combining characters (Arabic diacritics, etc.)."""
    return "".join(
        ch for ch in unicodedata.normalize("NFD", text)
        if unicodedata.category(ch) != "Mn"
    )


def _normalize(text: str) -> str:
    """Lower-case + strip diacritics + collapse whitespace."""
    return " ".join(_strip_diacritics(text).lower().split())


def _hr(char: str = "─", width: int = 64) -> str:
    return char * width


def _section(title: str) -> None:
    print(f"\n{_hr()}")
    print(f"  {title}")
    print(_hr())


def _physical_path(relative_path: Optional[str]) -> Optional[str]:
    """Resolve a DB-stored relative path (relative to backend/) to an absolute path."""
    if not relative_path:
        return None
    return os.path.join(_backend_dir, relative_path)


# ── Exam lookup (fuzzy: strips diacritics, case-insensitive) ─────────────────

def find_exam(db, exam_name: str, section: str) -> Optional[Exam]:
    """
    Find an exam by name and section using normalised comparison.
    The DB may store sections with Arabic diacritics (e.g. 'ِEA'), so we
    normalise both sides before comparing.
    """
    norm_name    = _normalize(exam_name)
    norm_section = _normalize(section)

    candidates = db.query(Exam).all()
    matches = [
        ex for ex in candidates
        if _normalize(ex.name) == norm_name
        and _normalize(ex.section) == norm_section
    ]

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        # Prefer non-archived
        active = [ex for ex in matches if not ex.is_archived]
        if len(active) == 1:
            return active[0]
        ids = ", ".join(str(ex.id) for ex in matches)
        print(
            f"[ERROR] Ambiguous: {len(matches)} exams match "
            f"name={exam_name!r} section={section!r} (ids: {ids}).\n"
            f"        Use delete_student_paper.py --exam-id <id> to be explicit."
        )
        return None
    return None


# ── Student extraction lookup ────────────────────────────────────────────────

def find_extraction(db, exam_id: int, student_number: str) -> Optional[StudentExtraction]:
    """Find the StudentExtraction for this student in this exam."""
    return (
        db.query(StudentExtraction)
        .filter(
            StudentExtraction.exam_id       == exam_id,
            StudentExtraction.student_id_num == student_number,
        )
        .first()
    )


# ── Collect everything that will be affected ─────────────────────────────────

def collect(db, exam: Exam, extraction: StudentExtraction, answer_file: Optional[AnswerFile]) -> dict:
    """
    Gather every DB row and every physical file that belongs to this student.
    Returns a plain dict so the caller can print it and/or delete it.
    """
    ext_id = extraction.id

    # ExamResult is keyed by (exam_id, student_id) where student_id is the
    # student_id_num or "STU-{ext.id:03d}" — same formula as the grading engine.
    student_id_str = extraction.student_id_num or f"STU-{extraction.id:03d}"
    exam_result = (
        db.query(ExamResult)
        .filter(
            ExamResult.exam_id    == exam.id,
            ExamResult.student_id == student_id_str,
        )
        .first()
    )

    question_scores = []
    if exam_result:
        question_scores = (
            db.query(StudentQuestionScore)
            .filter(StudentQuestionScore.result_id == exam_result.id)
            .all()
        )

    final_scores = (
        db.query(GradingFinalScore)
        .filter(GradingFinalScore.extraction_id == ext_id)
        .all()
    )

    model_results = (
        db.query(ModelGradingResult)
        .filter(ModelGradingResult.extraction_id == ext_id)
        .all()
    )

    answer_items = (
        db.query(StudentAnswerItem)
        .filter(StudentAnswerItem.extraction_id == ext_id)
        .all()
    )

    # Physical files
    pdf_path  = _physical_path(answer_file.file_path if answer_file else None)
    json_path = _physical_path(extraction.output_json_path)

    return {
        "exam":                  exam,
        "extraction":            extraction,
        "answer_file":           answer_file,
        "exam_result":           exam_result,
        "question_scores":       question_scores,
        "final_scores":          final_scores,
        "model_results":         model_results,
        "answer_items":          answer_items,
        "student_id_str":        student_id_str,
        # physical files — None if path not set, or absolute path string
        "pdf_path":              pdf_path,
        "pdf_exists":            bool(pdf_path and os.path.isfile(pdf_path)),
        "json_path":             json_path,
        "json_exists":           bool(json_path and os.path.isfile(json_path)),
    }


# ── Print summary ─────────────────────────────────────────────────────────────

def print_summary(d: dict) -> None:
    exam       = d["exam"]
    ext        = d["extraction"]
    af         = d["answer_file"]
    er         = d["exam_result"]

    _section("EXAM  (will NOT be deleted)")
    print(f"  id       : {exam.id}")
    print(f"  name     : {exam.name}")
    print(f"  section  : {exam.section}")
    print(f"  type     : {exam.type}")
    print(f"  status   : {exam.status}")

    _section("STUDENT IDENTITY")
    print(f"  student_id_num  : {ext.student_id_num}")
    print(f"  student_name    : {ext.student_name}")
    print(f"  student_section : {ext.student_section}")
    print(f"  student_id_str  : {d['student_id_str']}  (used in exam_results)")

    _section("ANSWER FILE  →  answer_files  (will be DELETED)")
    if af:
        print(f"  id          : {af.id}")
        print(f"  file_name   : {af.file_name}")
        print(f"  file_path   : {af.file_path}")
        print(f"  uploaded_at : {af.uploaded_at}")
    else:
        print("  (none — file was never uploaded or already removed)")

    _section("PHYSICAL PDF  (will be DELETED if exists)")
    pdf = d["pdf_path"]
    if pdf:
        exists_label = "EXISTS  ✓" if d["pdf_exists"] else "NOT FOUND  (skipped)"
        print(f"  path   : {pdf}")
        print(f"  status : {exists_label}")
    else:
        print("  (no file_path stored — nothing to remove)")

    _section("STUDENT EXTRACTION  →  student_extractions  (will be DELETED)")
    print(f"  id               : {ext.id}")
    print(f"  answer_file_id   : {ext.answer_file_id}")
    print(f"  output_json_path : {ext.output_json_path}")
    print(f"  extracted_at     : {ext.extracted_at}")

    _section("EXTRACTION JSON  (will be DELETED if exists)")
    jp = d["json_path"]
    if jp:
        exists_label = "EXISTS  ✓" if d["json_exists"] else "NOT FOUND  (skipped)"
        print(f"  path   : {jp}")
        print(f"  status : {exists_label}")
    else:
        print("  (no output_json_path stored — nothing to remove)")

    _section("STUDENT ANSWER ITEMS  →  student_answer_items")
    items = d["answer_items"]
    print(f"  {len(items)} row(s) will be deleted")
    for item in items:
        print(f"    id={item.id}  question_id={item.question_id}")

    _section("MODEL GRADING RESULTS  →  model_grading_results")
    mgr = d["model_results"]
    if mgr:
        # Group by job for readability
        by_job: dict[int, list] = {}
        for r in mgr:
            by_job.setdefault(r.grading_job_id, []).append(r)
        print(f"  {len(mgr)} row(s) across {len(by_job)} grading job(s)")
        for job_id, rows in sorted(by_job.items()):
            print(f"    job_id={job_id}: {len(rows)} row(s)")
    else:
        print("  (none)")

    _section("GRADING FINAL SCORES  →  grading_final_scores")
    fs = d["final_scores"]
    if fs:
        by_job2: dict[int, list] = {}
        for r in fs:
            by_job2.setdefault(r.grading_job_id, []).append(r)
        print(f"  {len(fs)} row(s) across {len(by_job2)} grading job(s)")
        for job_id, rows in sorted(by_job2.items()):
            scores_str = "  ".join(
                f"{r.question_id}={r.final_score}/{r.max_score}" for r in rows
            )
            print(f"    job_id={job_id}: {len(rows)} row(s)  [{scores_str}]")
    else:
        print("  (none)")

    _section("EXAM RESULT  →  exam_results")
    if er:
        print(f"  id           : {er.id}")
        print(f"  student_id   : {er.student_id}")
        print(f"  student_name : {er.student_name}")
        print(f"  total_score  : {er.total_score}")
        print(f"  grade        : {er.grade}")
        print(f"  status       : {er.status}")
    else:
        print("  (none — student was never graded)")

    _section("STUDENT QUESTION SCORES  →  student_question_scores")
    qs = d["question_scores"]
    if qs:
        print(f"  {len(qs)} row(s) will be deleted")
        for r in qs:
            print(f"    id={r.id}  q={r.question_number}  score={r.score}/{r.max_score}")
    else:
        print("  (none)")

    print(f"\n{_hr('═')}")
    print(f"  TOTAL  DB rows to delete:")
    print(f"    student_question_scores  : {len(d['question_scores'])}")
    print(f"    exam_results             : {'1' if er else '0'}")
    print(f"    grading_final_scores     : {len(d['final_scores'])}")
    print(f"    model_grading_results    : {len(d['model_results'])}")
    print(f"    student_answer_items     : {len(d['answer_items'])}")
    print(f"    student_extractions      : 1")
    print(f"    answer_files             : {'1' if af else '0'}")
    print(f"  Physical files to delete:")
    print(f"    answer PDF               : {'yes' if d['pdf_exists'] else 'no / not found'}")
    print(f"    extraction JSON          : {'yes' if d['json_exists'] else 'no / not found'}")
    print(_hr("═"))


# ── Execute deletions ─────────────────────────────────────────────────────────

def do_delete(db, d: dict) -> None:
    """
    Delete all rows in child-first order inside the caller's transaction.
    Physical files are removed after the DB commit succeeds (see main()).
    """
    er  = d["exam_result"]
    ext = d["extraction"]
    af  = d["answer_file"]

    # 1. StudentQuestionScore  (child of ExamResult)
    if er is not None:
        n = (
            db.query(StudentQuestionScore)
            .filter(StudentQuestionScore.result_id == er.id)
            .delete(synchronize_session=False)
        )
        print(f"  deleted {n} StudentQuestionScore row(s)")

    # 2. ExamResult
    if er is not None:
        db.delete(er)
        db.flush()
        print(f"  deleted ExamResult id={er.id}")

    # 3. GradingFinalScore  (child of StudentExtraction)
    n = (
        db.query(GradingFinalScore)
        .filter(GradingFinalScore.extraction_id == ext.id)
        .delete(synchronize_session=False)
    )
    print(f"  deleted {n} GradingFinalScore row(s)")

    # 4. ModelGradingResult  (child of StudentExtraction)
    n = (
        db.query(ModelGradingResult)
        .filter(ModelGradingResult.extraction_id == ext.id)
        .delete(synchronize_session=False)
    )
    print(f"  deleted {n} ModelGradingResult row(s)")

    # 5. StudentAnswerItem  (child of StudentExtraction)
    n = (
        db.query(StudentAnswerItem)
        .filter(StudentAnswerItem.extraction_id == ext.id)
        .delete(synchronize_session=False)
    )
    print(f"  deleted {n} StudentAnswerItem row(s)")

    # 6. StudentExtraction
    db.delete(ext)
    db.flush()
    print(f"  deleted StudentExtraction id={ext.id}")

    # 7. AnswerFile  (after extraction is gone; FK was SET NULL so no constraint issue)
    if af is not None:
        db.delete(af)
        db.flush()
        print(f"  deleted AnswerFile id={af.id}")


def delete_physical_files(d: dict) -> None:
    """Remove physical files from disk.  Called only after DB commit succeeds."""
    for label, path_key, exists_key in [
        ("answer PDF",       "pdf_path",  "pdf_exists"),
        ("extraction JSON",  "json_path", "json_exists"),
    ]:
        path   = d[path_key]
        exists = d[exists_key]
        if path and exists:
            try:
                os.remove(path)
                print(f"  deleted {label}: {path}")
            except OSError as exc:
                print(f"  [WARN] could not delete {label}: {exc}")
        elif path:
            print(f"  skipped {label} (not found on disk): {path}")
        else:
            print(f"  skipped {label} (no path stored)")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Delete all DB records and physical files for one student paper, "
            "located by student number + exam name + section."
        )
    )
    parser.add_argument(
        "--student-number", required=True,
        help="The student's ID number as stored on the exam cover page (e.g. 1846816)",
    )
    parser.add_argument(
        "--exam", required=True,
        help="Exam name (case-insensitive, e.g. 'Final')",
    )
    parser.add_argument(
        "--section", required=True,
        help="Exam section (diacritics-insensitive, e.g. 'EA')",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be deleted without touching the DB or files",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        # ── 1. Locate exam ────────────────────────────────────────────────
        exam = find_exam(db, args.exam, args.section)
        if exam is None:
            print(
                f"[ERROR] No exam found matching name={args.exam!r} "
                f"section={args.section!r}"
            )
            print("\nAvailable exams:")
            for ex in db.query(Exam).order_by(Exam.id).all():
                print(f"  id={ex.id}  name={ex.name!r}  section={ex.section!r}  type={ex.type}")
            sys.exit(1)

        print(f"\n[OK] Found exam: id={exam.id}  name={exam.name!r}  section={exam.section!r}")

        # ── 2. Locate student extraction ──────────────────────────────────
        extraction = find_extraction(db, exam.id, args.student_number)
        if extraction is None:
            print(
                f"[ERROR] No StudentExtraction found for student_number="
                f"{args.student_number!r} in exam_id={exam.id}"
            )
            print("\nStudents in this exam:")
            all_exts = (
                db.query(StudentExtraction)
                .filter(StudentExtraction.exam_id == exam.id)
                .all()
            )
            for e in all_exts:
                print(
                    f"  extraction_id={e.id}  "
                    f"student_id_num={e.student_id_num!r}  "
                    f"name={e.student_name!r}"
                )
            sys.exit(1)

        print(
            f"[OK] Found student: extraction_id={extraction.id}  "
            f"name={extraction.student_name!r}  "
            f"id_num={extraction.student_id_num!r}"
        )

        # ── 3. Resolve answer file ────────────────────────────────────────
        answer_file = None
        if extraction.answer_file_id is not None:
            answer_file = db.get(AnswerFile, extraction.answer_file_id)

        # ── 4. Collect everything ─────────────────────────────────────────
        d = collect(db, exam, extraction, answer_file)

        # ── 5. Print full plan ────────────────────────────────────────────
        mode_label = "[DRY-RUN — nothing will be changed]" if args.dry_run else "[LIVE DELETE]"
        print(f"\n{'─' * 64}")
        print(f"  {mode_label}")
        print_summary(d)

        if args.dry_run:
            print("\n[DRY-RUN] No changes made.  Re-run without --dry-run to proceed.")
            return

        # ── 6. Confirm ────────────────────────────────────────────────────
        print(
            f"\nThis will permanently delete the student record for "
            f"{extraction.student_name!r} (#{extraction.student_id_num}) "
            f"from exam '{exam.name}'."
        )
        answer = input("Type 'yes' to confirm deletion: ").strip().lower()
        if answer != "yes":
            print("Aborted — no changes made.")
            return

        # ── 7. DB transaction ─────────────────────────────────────────────
        print("\nDeleting DB rows…")
        try:
            do_delete(db, d)
            db.commit()
            print("\n[OK] DB transaction committed.")
        except Exception as exc:
            db.rollback()
            print(f"\n[ERROR] Transaction failed — rolled back.\n  {exc}")
            raise

        # ── 8. Physical files (after commit — so DB is clean even if this fails) ──
        print("\nDeleting physical files…")
        delete_physical_files(d)

        print(f"\n[DONE] Student {extraction.student_name!r} (#{extraction.student_id_num}) "
              f"fully removed from exam '{exam.name}'.")

    finally:
        db.close()


if __name__ == "__main__":
    main()
