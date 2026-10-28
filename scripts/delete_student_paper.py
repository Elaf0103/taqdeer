"""
delete_student_paper.py — dev/admin cleanup script
====================================================
Deletes exactly one student paper's database records for a given exam.

Usage
-----
  # Dry-run (prints what would be deleted, touches nothing):
  python scripts/delete_student_paper.py --exam-id 21 --answer-file-id 7 --dry-run
  python scripts/delete_student_paper.py --exam-id 21 --extraction-id 42 --dry-run

  # Live delete:
  python scripts/delete_student_paper.py --exam-id 21 --answer-file-id 7
  python scripts/delete_student_paper.py --exam-id 21 --extraction-id 42

Run from the project root (taqdeer/), not from inside backend/.

What is deleted (in order):
  1. StudentQuestionScore rows  (child of ExamResult)
  2. ExamResult row             (matched by exam_id + student_id string)
  3. GradingFinalScore rows     (child of StudentExtraction)
  4. ModelGradingResult rows    (child of StudentExtraction)
  5. StudentAnswerItem rows     (child of StudentExtraction)
  6. StudentExtraction row      (the extraction itself)
  7. AnswerFile row             (the uploaded PDF record)

What is NOT deleted:
  - Exam, Rubric, ExamModelAnswer (answer key)
  - Any other student's records
  - GradingJob rows (shared across students; remains intact)
  - GradingReport rows (shared across the exam; remains intact)
  - ExamGapAnalysis (shared across the exam; remains intact)
"""

import argparse
import sys
import os

# ── Make backend importable when running from project root ──────────────────
_script_dir = os.path.dirname(os.path.abspath(__file__))
_backend_dir = os.path.join(_script_dir, "..", "backend")
sys.path.insert(0, os.path.abspath(_backend_dir))

from dotenv import load_dotenv
from pathlib import Path

load_dotenv(dotenv_path=Path(_backend_dir) / ".env", override=True)

from database import SessionLocal
from models import (
    AnswerFile,
    StudentExtraction,
    StudentAnswerItem,
    ModelGradingResult,
    GradingFinalScore,
    ExamResult,
    StudentQuestionScore,
    Exam,
)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _student_id_string(ext: StudentExtraction) -> str:
    """
    Replicates the grading engine's formula for ExamResult.student_id.
    See grading_engine.py line ~882 and ~899.
    """
    return ext.student_id_num if ext.student_id_num else f"STU-{ext.id:03d}"


def _hr(char: str = "─", width: int = 60) -> str:
    return char * width


def _section(title: str) -> None:
    print(f"\n{_hr()}")
    print(f"  {title}")
    print(_hr())


# ── Core logic ───────────────────────────────────────────────────────────────

def resolve_targets(
    db,
    exam_id: int,
    answer_file_id: int | None,
    extraction_id: int | None,
) -> tuple[AnswerFile | None, StudentExtraction | None]:
    """
    Resolve to (answer_file, extraction) from whichever ID was supplied.
    Returns (None, None) if nothing is found and prints an error.
    """
    answer_file = None
    extraction  = None

    if extraction_id is not None:
        extraction = db.get(StudentExtraction, extraction_id)
        if extraction is None:
            print(f"[ERROR] No StudentExtraction found with id={extraction_id}")
            return None, None
        if extraction.exam_id != exam_id:
            print(
                f"[ERROR] extraction_id={extraction_id} belongs to exam_id="
                f"{extraction.exam_id}, not exam_id={exam_id}"
            )
            return None, None
        if extraction.answer_file_id is not None:
            answer_file = db.get(AnswerFile, extraction.answer_file_id)

    elif answer_file_id is not None:
        answer_file = db.get(AnswerFile, answer_file_id)
        if answer_file is None:
            print(f"[ERROR] No AnswerFile found with id={answer_file_id}")
            return None, None
        if answer_file.exam_id != exam_id:
            print(
                f"[ERROR] answer_file_id={answer_file_id} belongs to exam_id="
                f"{answer_file.exam_id}, not exam_id={exam_id}"
            )
            return None, None
        extraction = (
            db.query(StudentExtraction)
            .filter(StudentExtraction.answer_file_id == answer_file_id)
            .first()
        )
        # extraction may be None if the file was uploaded but never extracted

    return answer_file, extraction


def collect_rows(
    db,
    exam_id: int,
    answer_file: AnswerFile | None,
    extraction: StudentExtraction | None,
) -> dict:
    """
    Gather all rows that will be deleted and return them in a structured dict.
    """
    rows: dict = {
        "answer_file":           answer_file,
        "extraction":            extraction,
        "answer_items":          [],
        "model_grading_results": [],
        "final_scores":          [],
        "exam_result":           None,
        "question_scores":       [],
    }

    if extraction is not None:
        rows["answer_items"] = (
            db.query(StudentAnswerItem)
            .filter(StudentAnswerItem.extraction_id == extraction.id)
            .all()
        )
        rows["model_grading_results"] = (
            db.query(ModelGradingResult)
            .filter(ModelGradingResult.extraction_id == extraction.id)
            .all()
        )
        rows["final_scores"] = (
            db.query(GradingFinalScore)
            .filter(GradingFinalScore.extraction_id == extraction.id)
            .all()
        )

        # ExamResult is linked via the student_id string, not a FK
        sid = _student_id_string(extraction)
        exam_result = (
            db.query(ExamResult)
            .filter(ExamResult.exam_id == exam_id, ExamResult.student_id == sid)
            .first()
        )
        rows["exam_result"] = exam_result

        if exam_result is not None:
            rows["question_scores"] = (
                db.query(StudentQuestionScore)
                .filter(StudentQuestionScore.result_id == exam_result.id)
                .all()
            )

    return rows


def print_plan(exam: Exam, rows: dict) -> None:
    """Print a human-readable summary of what will be deleted."""
    _section("EXAM")
    print(f"  exam_id   : {exam.id}")
    print(f"  exam_name : {exam.name}")

    af = rows["answer_file"]
    ext = rows["extraction"]

    _section("ANSWER FILE  (answer_files)")
    if af:
        print(f"  id          : {af.id}")
        print(f"  file_name   : {af.file_name}")
        print(f"  file_path   : {af.file_path}")
        print(f"  uploaded_at : {af.uploaded_at}")
    else:
        print("  (none — no AnswerFile row found or not linked)")

    _section("STUDENT EXTRACTION  (student_extractions)")
    if ext:
        print(f"  id              : {ext.id}")
        print(f"  student_name    : {ext.student_name}")
        print(f"  student_id_num  : {ext.student_id_num}")
        print(f"  student_id str  : {_student_id_string(ext)}")
        print(f"  answer_file_id  : {ext.answer_file_id}")
        print(f"  extracted_at    : {ext.extracted_at}")
    else:
        print("  (none — file was uploaded but never extracted)")

    _section("STUDENT ANSWER ITEMS  (student_answer_items)")
    items = rows["answer_items"]
    if items:
        print(f"  {len(items)} row(s) will be deleted")
        for item in items:
            print(f"    id={item.id}  question_id={item.question_id}")
    else:
        print("  (none)")

    _section("MODEL GRADING RESULTS  (model_grading_results)")
    mgr_rows = rows["model_grading_results"]
    if mgr_rows:
        print(f"  {len(mgr_rows)} row(s) will be deleted")
        for r in mgr_rows:
            print(
                f"    id={r.id}  job={r.grading_job_id}"
                f"  q={r.question_id}  model={r.model_name}  phase={r.phase}"
            )
    else:
        print("  (none)")

    _section("GRADING FINAL SCORES  (grading_final_scores)")
    fs_rows = rows["final_scores"]
    if fs_rows:
        print(f"  {len(fs_rows)} row(s) will be deleted")
        for r in fs_rows:
            print(
                f"    id={r.id}  job={r.grading_job_id}"
                f"  q={r.question_id}  score={r.final_score}/{r.max_score}"
            )
    else:
        print("  (none)")

    _section("EXAM RESULT  (exam_results)")
    er = rows["exam_result"]
    if er:
        print(f"  id           : {er.id}")
        print(f"  student_id   : {er.student_id}")
        print(f"  student_name : {er.student_name}")
        print(f"  total_score  : {er.total_score}")
        print(f"  grade        : {er.grade}")
        print(f"  status       : {er.status}")
    else:
        print("  (none — student was never graded)")

    _section("STUDENT QUESTION SCORES  (student_question_scores)")
    qs_rows = rows["question_scores"]
    if qs_rows:
        print(f"  {len(qs_rows)} row(s) will be deleted")
        for r in qs_rows:
            print(
                f"    id={r.id}  q={r.question_number}"
                f"  score={r.score}/{r.max_score}"
            )
    else:
        print("  (none)")

    print(f"\n{_hr('═')}")


def do_delete(db, rows: dict) -> None:
    """
    Execute the deletions in child-first order inside the caller's transaction.
    The caller is responsible for commit / rollback.
    """
    # 1. StudentQuestionScore (child of ExamResult)
    er = rows["exam_result"]
    if er is not None:
        deleted = (
            db.query(StudentQuestionScore)
            .filter(StudentQuestionScore.result_id == er.id)
            .delete(synchronize_session=False)
        )
        print(f"  deleted {deleted} StudentQuestionScore row(s)")

        # 2. ExamResult
        db.delete(er)
        db.flush()
        print(f"  deleted ExamResult id={er.id}")

    # 3. GradingFinalScore
    ext = rows["extraction"]
    if ext is not None:
        deleted = (
            db.query(GradingFinalScore)
            .filter(GradingFinalScore.extraction_id == ext.id)
            .delete(synchronize_session=False)
        )
        print(f"  deleted {deleted} GradingFinalScore row(s)")

        # 4. ModelGradingResult
        deleted = (
            db.query(ModelGradingResult)
            .filter(ModelGradingResult.extraction_id == ext.id)
            .delete(synchronize_session=False)
        )
        print(f"  deleted {deleted} ModelGradingResult row(s)")

        # 5. StudentAnswerItem
        deleted = (
            db.query(StudentAnswerItem)
            .filter(StudentAnswerItem.extraction_id == ext.id)
            .delete(synchronize_session=False)
        )
        print(f"  deleted {deleted} StudentAnswerItem row(s)")

        # 6. StudentExtraction
        db.delete(ext)
        db.flush()
        print(f"  deleted StudentExtraction id={ext.id}")

    # 7. AnswerFile
    af = rows["answer_file"]
    if af is not None:
        db.delete(af)
        db.flush()
        print(f"  deleted AnswerFile id={af.id}")


# ── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Delete one student paper's DB records for a given exam."
    )
    parser.add_argument("--exam-id",        type=int, required=True,
                        help="ID of the exam this paper belongs to")

    id_group = parser.add_mutually_exclusive_group(required=True)
    id_group.add_argument("--answer-file-id", type=int,
                          help="ID of the AnswerFile row to delete")
    id_group.add_argument("--extraction-id",  type=int,
                          help="ID of the StudentExtraction row to delete")

    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be deleted without touching the DB")

    args = parser.parse_args()

    db = SessionLocal()
    try:
        # ── 1. Verify exam exists ──────────────────────────────────────────
        exam = db.get(Exam, args.exam_id)
        if exam is None:
            print(f"[ERROR] No Exam found with id={args.exam_id}")
            sys.exit(1)

        # ── 2. Resolve answer_file + extraction ───────────────────────────
        answer_file, extraction = resolve_targets(
            db,
            exam_id        = args.exam_id,
            answer_file_id = args.answer_file_id,
            extraction_id  = args.extraction_id,
        )
        if answer_file is None and extraction is None:
            sys.exit(1)

        # ── 3. Collect all rows that will be affected ─────────────────────
        rows = collect_rows(db, args.exam_id, answer_file, extraction)

        # ── 4. Print the plan ─────────────────────────────────────────────
        if args.dry_run:
            print("\n[DRY-RUN MODE — nothing will be deleted]\n")
        else:
            print("\n[LIVE DELETE — changes will be committed]\n")

        print_plan(exam, rows)

        # ── 5. Nothing to do? ─────────────────────────────────────────────
        if answer_file is None and extraction is None:
            print("Nothing to delete.")
            return

        # ── 6. Dry-run: stop here ─────────────────────────────────────────
        if args.dry_run:
            print("[DRY-RUN] No changes made.")
            return

        # ── 7. Confirm before live delete ─────────────────────────────────
        answer = input("\nProceed with deletion? [yes/no]: ").strip().lower()
        if answer != "yes":
            print("Aborted — no changes made.")
            return

        # ── 8. Execute inside a transaction ──────────────────────────────
        print("\nDeleting…")
        try:
            do_delete(db, rows)
            db.commit()
            print("\n[OK] Deletion committed successfully.")
        except Exception as exc:
            db.rollback()
            print(f"\n[ERROR] Deletion failed — transaction rolled back.\n  {exc}")
            raise

    finally:
        db.close()


if __name__ == "__main__":
    main()
