"""
Gap Analysis Engine
===================
Aggregates per-question performance statistics from the database, calls Qwen,
and returns a structured learning-gap report.

Two entry points:

  run_gap_analysis(db, exam_id)
      Core analysis: aggregate DB data, call Qwen, return result dict.
      Called by the router (on-demand) and by trigger_gap_analysis().

  trigger_gap_analysis(exam_id, db=None)
      Auto-trigger entry point called at the end of grading.
      Runs run_gap_analysis() then upserts the JSON result into
      exam_gap_analysis so the learning-gaps page loads from cache.

Data flow:
  DB (exam_model_answers + exam_results + student_question_scores)
    → aggregate per-question stats
    → call Qwen (QwenGradingClient)
    → parse JSON response
    → store in exam_gap_analysis
    → return GapAnalysisReport dict
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from database import SessionLocal
from models import Exam, ExamGapAnalysis, ExamModelAnswer, ExamResult, StudentQuestionScore
from services.gap_analysis_prompt import build_gap_analysis_prompt
from services.grading_models import QwenGradingClient


# ── LLM client (singleton per call — stateless) ───────────────────────────────

def _get_qwen_client() -> QwenGradingClient:
    return QwenGradingClient()


# ── Aggregation helpers ───────────────────────────────────────────────────────

def _count_question_units(model_answer: str) -> int:
    """
    Return the number of grading units the grading engine produces for this
    model answer row.  A grouped question ({"__sub__": [...]}) produces one
    unit per sub-entry; everything else produces exactly one unit.
    """
    if model_answer and model_answer.strip().startswith('{"__sub__":'):
        try:
            obj = json.loads(model_answer)
            sub_list = obj.get("__sub__", [])
            if isinstance(sub_list, list) and sub_list:
                return len(sub_list)
        except (json.JSONDecodeError, TypeError):
            pass
    return 1


def _build_sqs_to_main_mapping(q_rows: list[ExamModelAnswer]) -> dict[int, int]:
    """
    Map each sequential StudentQuestionScore.question_number back to the real
    ExamModelAnswer.question_number (the main exam question number).

    The grading engine assigns question_number via enumerate(question_units, 1),
    flattening all sub-questions into a single sequential list.  This function
    reconstructs that same flattening order — using the ExamModelAnswer rows
    sorted by question_number — and builds the reverse lookup.

    Example:
        EMA Q1 (1 unit)  → SQS question_numbers 1      → main Q1
        EMA Q2 (3 units) → SQS question_numbers 2, 3, 4 → main Q2
        EMA Q3 (2 units) → SQS question_numbers 5, 6    → main Q3
    """
    mapping: dict[int, int] = {}
    seq = 1
    for row in q_rows:          # already ordered by question_number ascending
        n = _count_question_units(row.model_answer or "")
        for _ in range(n):
            mapping[seq] = row.question_number
            seq += 1
    return mapping


def _aggregate_question_stats(
    db: Session,
    exam_id: int,
) -> tuple[list[dict], int]:
    """
    Return (question_stats_list, n_students).

    question_stats_list — one dict per MAIN exam question (not per grading unit),
    sorted ascending by question_number:
        question_number, question_text, answer_key_summary,
        max_score, class_average_pct, number_of_students,
        band_0_20, band_21_40, band_41_60, band_61_80, band_81_100

    Sub-questions are aggregated under their parent question so the analytics
    reflect the exam structure (Q1, Q2, Q3 …) rather than internal grading
    fragments (Q2a, Q2b, Q2c stored as sequential integers 2, 3, 4 …).

    n_students — number of distinct graded students for this exam
    """
    # Load all graded results for this exam
    results: list[ExamResult] = (
        db.query(ExamResult)
        .filter(ExamResult.exam_id == exam_id)
        .all()
    )
    if not results:
        return [], 0

    result_ids = [r.id for r in results]
    n_students = len(results)

    # Load all per-question scores for these results
    all_sqs: list[StudentQuestionScore] = (
        db.query(StudentQuestionScore)
        .filter(StudentQuestionScore.result_id.in_(result_ids))
        .all()
    )

    if not all_sqs:
        return [], n_students

    # Load model answer rows — source of truth for the real question numbers
    # and sub-question structure.
    q_rows: list[ExamModelAnswer] = (
        db.query(ExamModelAnswer)
        .filter(ExamModelAnswer.exam_id == exam_id)
        .order_by(ExamModelAnswer.question_number)
        .all()
    )
    # Keyed by actual question_number (1, 2, 3 …)
    q_meta: dict[int, ExamModelAnswer] = {r.question_number: r for r in q_rows}
    # Max marks per main question (from ExamModelAnswer.score, not SQS.max_score)
    main_max: dict[int, float] = {r.question_number: float(r.score or 1) for r in q_rows}

    # Map sequential SQS question_number → actual main question_number.
    # If q_rows is empty (edge-case: no model answers defined), fall back to
    # identity mapping so we still return something rather than crash.
    sqs_to_main = _build_sqs_to_main_mapping(q_rows) if q_rows else {}

    # ── Step 1: sum per-student scores under each main question ───────────────
    # key: (result_id, main_question_number)  value: cumulative raw score
    student_scores: dict[tuple[int, int], float] = defaultdict(float)

    for sqs in all_sqs:
        main_qn = sqs_to_main.get(sqs.question_number)
        if main_qn is None:
            # Sequential number not in mapping (data predates sub-question
            # grading, or mapping is out of sync).  Keep as-is.
            main_qn = sqs.question_number
        student_scores[(sqs.result_id, main_qn)] += float(sqs.score)

    # ── Step 2: convert cumulative scores to percentages and band them ────────
    by_qnum: dict[int, dict] = {}

    for (result_id, main_qn), total_score in student_scores.items():
        if main_qn not in by_qnum:
            row  = q_meta.get(main_qn)
            by_qnum[main_qn] = {
                "question_number":    main_qn,
                "question_text":      row.question_text if row else f"Question {main_qn}",
                "answer_key_summary": _summarise_answer_key(row.model_answer if row else ""),
                "max_score":          main_max.get(main_qn, 1.0) or 1.0,
                "scores":             [],
            }
        q_max = by_qnum[main_qn]["max_score"]
        pct   = total_score / q_max * 100
        by_qnum[main_qn]["scores"].append(pct)

    # ── Step 3: compute statistics and build output list ─────────────────────
    stats: list[dict] = []
    for qn in sorted(by_qnum.keys()):
        entry  = by_qnum[qn]
        scores = entry["scores"]
        n      = len(scores) or 1
        avg    = sum(scores) / n

        bands = [0, 0, 0, 0, 0]
        for p in scores:
            if   p <= 20: bands[0] += 1
            elif p <= 40: bands[1] += 1
            elif p <= 60: bands[2] += 1
            elif p <= 80: bands[3] += 1
            else:         bands[4] += 1

        stats.append({
            "question_number":    qn,
            "question_text":      entry["question_text"],
            "answer_key_summary": entry["answer_key_summary"],
            "max_score":          entry["max_score"],
            "class_average_pct":  round(avg, 1),
            "number_of_students": n,
            "band_0_20":          bands[0],
            "band_21_40":         bands[1],
            "band_41_60":         bands[2],
            "band_61_80":         bands[3],
            "band_81_100":        bands[4],
        })

    print(
        f"[gap-analysis-agg] exam_id={exam_id} "
        f"main_questions={len(stats)} students={n_students} "
        f"(sub-question units collapsed into parent questions)"
    )
    return stats, n_students


def _summarise_answer_key(model_answer: str) -> str:
    """
    Return a short (≤ 200 char) summary of the model answer for the LLM context.
    Strips JSON structures (MCQ / grouped question wrappers) down to plain text.
    """
    if not model_answer:
        return "(not provided)"

    # Try to parse JSON
    if model_answer.strip().startswith("{") or model_answer.strip().startswith("["):
        try:
            obj = json.loads(model_answer)
            if isinstance(obj, dict):
                # Grouped question: {"__sub__": [...]}
                if "__sub__" in obj:
                    subs = obj["__sub__"]
                    parts = [f"{s.get('label','?')}) {s.get('answer_key','')}" for s in subs[:3]]
                    return "; ".join(parts)[:200]
                # MCQ: {"type":"multiple_choice","options":[...]}
                if obj.get("type") == "multiple_choice":
                    opts = obj.get("options", [])
                    correct = next((o for o in opts if o.get("correct")), None)
                    if correct:
                        return f"Correct option: {correct.get('key','?')} — {correct.get('text','')}"[:200]
                # True/False
                if obj.get("type") == "true_false":
                    return f"Answer: {'True' if obj.get('correct') else 'False'}"
        except (json.JSONDecodeError, TypeError):
            pass

    # Plain text: truncate
    text = model_answer.strip()
    return text[:200] + ("…" if len(text) > 200 else "")


# ── JSON response parsing ─────────────────────────────────────────────────────

def _parse_llm_response(raw: str) -> dict[str, Any]:
    """
    Extract and parse the JSON object from the LLM's response.
    Strips any accidental markdown fences or leading/trailing prose.
    Raises ValueError on complete parse failure.
    """
    # Strip markdown code fences
    cleaned = re.sub(r"```(?:json)?", "", raw).strip()

    # Try full parse first
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Try to fish out the first { … } block
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Could not parse LLM response as JSON. Raw: {raw[:300]}")


def _validate_and_fill(
    parsed: dict[str, Any],
    question_stats: list[dict],
) -> dict[str, Any]:
    """
    Ensure the parsed JSON has the required keys and that every question
    in question_stats appears in parsed["questions"].  Missing questions
    get a fallback entry with severity NONE.
    """
    if "questions" not in parsed:
        parsed["questions"] = []
    if "clusters" not in parsed:
        parsed["clusters"] = []
    if "section_summary" not in parsed:
        parsed["section_summary"] = {
            "top_3_weaknesses":  [],
            "top_3_strengths":   [],
            "instructional_plan": "",
        }

    # Index existing question entries
    existing = {q.get("question_number"): q for q in parsed["questions"]}

    # Fill in any missing questions
    filled: list[dict] = []
    for stat in question_stats:
        qn  = stat["question_number"]
        entry = existing.get(qn) or {
            "question_number":       qn,
            "gap_detected":          False,
            "severity":              "NONE",
            "issue_type":            "none",
            "conceptual_explanation": "",
            "intervention":          "",
        }
        # Ensure required keys
        entry.setdefault("gap_detected",          False)
        entry.setdefault("severity",              "NONE")
        entry.setdefault("issue_type",            "none")
        entry.setdefault("conceptual_explanation", "")
        entry.setdefault("intervention",          "")
        filled.append(entry)

    parsed["questions"] = sorted(filled, key=lambda x: x.get("question_number", 0))
    return parsed


# ── Public entry point ────────────────────────────────────────────────────────

def run_gap_analysis(db: Session, exam_id: int) -> dict[str, Any]:
    """
    Perform learning gap analysis for the given exam.

    Returns a dict with:
      exam_id          : int
      exam_name        : str
      n_students       : int
      question_stats   : list[dict]   — raw aggregated data (for the heatmap)
      gap_report       : dict         — LLM output (questions, clusters, section_summary)
      error            : str | None   — non-None if LLM call failed (fallback mode)

    Raises:
      ValueError — exam not found, or no graded data available
    """
    exam: Optional[Exam] = db.get(Exam, exam_id)
    if exam is None:
        raise ValueError(f"Exam {exam_id} not found.")

    question_stats, n_students = _aggregate_question_stats(db, exam_id)

    if not question_stats:
        raise ValueError(
            "No graded question data found for this exam. "
            "Run auto-grading first, then try again."
        )

    exam_name    = exam.name or f"Exam {exam_id}"
    exam_type    = exam.type.value if exam.type else "exam"
    exam_section = exam.section or "—"

    # Build the prompt
    prompt = build_gap_analysis_prompt(
        exam_name    = exam_name,
        exam_type    = exam_type,
        exam_section = exam_section,
        questions    = question_stats,
    )

    # Call Qwen
    llm_error: Optional[str] = None
    gap_report: dict[str, Any] = {}

    try:
        client = _get_qwen_client()
        print(f"[GAP-ANALYSIS-MODEL] provider=qwen model={client.name}")
        raw    = client.complete(prompt)
        parsed = _parse_llm_response(raw)
        gap_report = _validate_and_fill(parsed, question_stats)
        print(
            f"[gap-analysis] exam_id={exam_id} "
            f"questions={len(question_stats)} students={n_students} "
            f"gaps_detected={sum(1 for q in gap_report.get('questions',[]) if q.get('gap_detected'))}"
        )
    except Exception as exc:
        llm_error = str(exc)
        print(f"[gap-analysis] LLM call failed for exam_id={exam_id}: {exc}")
        # Return fallback with only statistical data
        gap_report = _validate_and_fill({}, question_stats)

    return {
        "exam_id":        exam_id,
        "exam_name":      exam_name,
        "exam_section":   exam_section,
        "exam_type":      exam_type,
        "n_students":     n_students,
        "question_stats": question_stats,
        "gap_report":     gap_report,
        "error":          llm_error,
    }


# ── Auto-trigger: called at the end of every grading job ─────────────────────

def trigger_gap_analysis(exam_id: int, db: Optional[Session] = None) -> None:
    """
    Run gap analysis for exam_id and cache the result in exam_gap_analysis.

    Called automatically by grading_engine.run_grading_background() after a
    grading job completes.  Never raises — any failure is logged and swallowed
    so it cannot abort the grading job's completion path.

    If db is supplied it is used as-is (caller owns the session lifecycle).
    If db is None a new session is opened and closed here.
    """
    print(f"[GAP-ANALYSIS-AUTO-TRIGGERED] exam_id={exam_id}")

    owns_session = db is None
    if owns_session:
        db = SessionLocal()

    try:
        result = run_gap_analysis(db, exam_id)
        result_json = json.dumps(result, ensure_ascii=False)

        existing = (
            db.query(ExamGapAnalysis)
            .filter(ExamGapAnalysis.exam_id == exam_id)
            .first()
        )
        if existing:
            existing.analysis_json = result_json
            existing.generated_at  = datetime.utcnow()
            existing.model_name    = "qwen"
        else:
            db.add(ExamGapAnalysis(
                exam_id       = exam_id,
                analysis_json = result_json,
                model_name    = "qwen",
            ))
        db.commit()
        print(f"[gap-analysis] cached analysis stored for exam_id={exam_id}")

    except Exception as exc:
        print(f"[gap-analysis] auto-trigger failed for exam_id={exam_id}: {exc}")
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        if owns_session:
            try:
                db.close()
            except Exception:
                pass
