"""
Extraction router — /extraction
================================
New clean router for the answer-key and student-answer extraction pipelines.

This is NOT a restore of any prior extraction router.  It was written from
scratch to integrate with the new grading_notebook/exam_grading_system_generic.ipynb
pipeline via two external service modules:
  backend/services/answer_key_runner.py
  backend/services/student_extractor.py

The notebook file itself is never modified or executed here.

Marks policy (enforced throughout this router):
  Fields produced by the notebook that contain marks/scores are never read,
  stored, or returned:
    - examiner_marks_numeric
    - examiner_marks_raw
    - expected_marks
    - outcome_marks
    - total_marks
  Marks remain manual in the UI.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import List

from fastapi import (
    APIRouter, BackgroundTasks, Depends, File, HTTPException,
    UploadFile, status,
)
from sqlalchemy.orm import Session

from database import get_db
from deps import get_current_user
from models import (
    AnswerFile, Exam, ExamModelAnswer, ExtractionStatus,
    StudentAnswerItem, StudentExtraction, User,
)
from permissions import require_exam_access
from schemas import (
    AnswerKeyExtractionResponse,
    StudentAnswerItemRead,
    StudentAnswerItemUpdate,
    StudentExtractionRead,
    StudentExtractionWithAnswers,
)
from services.answer_key_runner import run_answer_key_extraction
from services.student_extractor import run_student_extraction

router = APIRouter(prefix="/extraction", tags=["Extraction"])

# Upload roots (relative to the backend/ working directory)
_AK_UPLOAD_DIR    = Path("uploads/answer_key")
_EXTR_OUTPUT_DIR  = Path("uploads/extractions")


# ── Answer-type taxonomy → UI internal code ─────────────────────────────────────

def _map_taxonomy_to_ui_type(answer_type: str, answer_subtype: str) -> str:
    """
    Convert notebook taxonomy strings to the UI's internal answer type codes.

    UI codes (must match the <select data-gp="type"> options in rubrics.html):
      recall_general | recall_differentiation | mathematical |
      diagrammatic_aoa | multiple_choice | true_false
    """
    t = (answer_type or "").strip()
    s = (answer_subtype or "").strip().lower()

    if t == "Selection-Based":
        if "true" in s and "false" in s:
            return "true_false"
        return "multiple_choice"

    if t == "Recall and Short Answer":
        if "differentiation" in s or "comparison" in s:
            return "recall_differentiation"
        return "recall_general"

    if t == "Mathematical and Computational":
        return "mathematical"

    if t == "Diagrammatic and Visual":
        if "aoa" in s or "aon" in s or "network" in s:
            return "diagrammatic_aoa"
        return "recall_general"

    # Code, Table, Algorithm, Analysis, Design, Essay → recall_general
    return "recall_general"


def _serialize_model_answer(sq: dict, ui_type: str) -> str:
    """
    Serialize the extracted answer into the format expected by the UI editor.

    • multiple_choice → {"type":"multiple_choice","allow_multiple":...,"options":[...]}
    • true_false      → {"type":"true_false","correct":true|false|null}
    • everything else → plain text (correct_answer string)
    """
    if ui_type == "multiple_choice":
        mcq_options = sq.get("mcq_options") or {}
        mcq_correct = (sq.get("mcq_correct_option") or "").strip().lower()
        subtype     = (sq.get("answer_subtype") or "").lower()
        allow_multi = "multi-select" in subtype or "multi select" in subtype

        options = []
        for key, text in sorted(mcq_options.items()):
            options.append({
                "key":     key.strip().lower(),
                "text":    str(text),
                "correct": key.strip().lower() == mcq_correct,
            })

        if not options:
            # No option dict available — fall back to plain text
            return sq.get("correct_answer") or ""

        return json.dumps(
            {"type": "multiple_choice", "allow_multiple": allow_multi, "options": options},
            ensure_ascii=False,
        )

    if ui_type == "true_false":
        raw = (sq.get("correct_answer") or sq.get("mcq_correct_option") or "").strip().lower()
        if raw in ("true", "t", "yes", "صح", "صواب", "1"):
            correct: bool | None = True
        elif raw in ("false", "f", "no", "خطأ", "خطا", "0"):
            correct = False
        else:
            correct = None
        return json.dumps({"type": "true_false", "correct": correct}, ensure_ascii=False)

    # Short answer / everything else
    return sq.get("correct_answer") or ""


_QNUM_LETTER_PAT = re.compile(r'^(Q\d+)([a-zA-Z]+)$')


def _regroup_by_qnum(questions: list[dict]) -> list[dict]:
    """
    Collapse questions whose question_number embeds the sub-letter into
    a proper grouped question.

    Example — runner produces three top-level questions because the LLM
    returned question_number = "Q1a", "Q1b", "Q1c" instead of "Q1":

        [{"question_number": "Q1a", "sub_questions": [{"sub_id":"a", ...}]},
         {"question_number": "Q1b", "sub_questions": [{"sub_id":"b", ...}]},
         {"question_number": "Q1c", "sub_questions": [{"sub_id":"c", ...}]}]

    becomes:

        [{"question_number": "Q1",
          "question_text": "<first entry's question_text>",
          "sub_questions": [
              {"sub_id":"a", ...},
              {"sub_id":"b", ...},
              {"sub_id":"c", ...},
          ]}]

    Questions that already have the correct structure (Q1 with sub_questions
    a/b/c already nested inside) are passed through unchanged — the pattern
    match only fires when the letter is part of question_number itself.

    Standalone numeric questions (Q1, Q2, Q3…) or MCQ-series with numeric
    suffixes are also passed through unchanged.
    """
    from collections import OrderedDict

    out: OrderedDict[str, dict] = OrderedDict()

    for q in questions:
        qnum = q.get("question_number", "")
        m = _QNUM_LETTER_PAT.match(qnum)

        if m:
            parent = m.group(1)   # "Q1"
            letter = m.group(2).lower()  # "a"

            if parent not in out:
                out[parent] = {
                    "question_number": parent,
                    "question_text":   (q.get("question_text") or "").strip(),
                    "sub_questions":   [],
                }

            # Adopt this question's sub_questions, re-stamping sub_id = letter
            for sq in q.get("sub_questions", []):
                out[parent]["sub_questions"].append({**sq, "sub_id": letter})
        else:
            # Non-suffixed question — pass through as-is
            if qnum not in out:
                out[qnum] = q
            else:
                # Duplicate key (shouldn't happen, but merge sub_questions defensively)
                out[qnum]["sub_questions"].extend(q.get("sub_questions", []))

    return list(out.values())


def _should_group(sub_qs: list[dict]) -> bool:
    """
    Decide whether a question's sub_questions should be stored as one grouped card.

    Grouped (parent card + sub-blocks):
      • More than one sub-question
      • AND sub_ids are alphabetic (a, b, c…) — indicates true sub-parts of one question
      • OR sub_ids are numeric AND every sub-question is Selection-Based (MCQ) —
        handles dot-numeric patterns like Q7.1/Q7.2/Q7.3/Q7.4 that belong to one stem

    Flat (individual cards):
      • Single sub-question  → no grouping needed
      • Numeric sub_ids (1, 2, 3…) that are NOT all MCQ → independent numbered items;
        each gets its own card
    """
    if len(sub_qs) <= 1:
        return False
    all_numeric = all(sq.get("sub_id", "x").isdigit() for sq in sub_qs)
    if not all_numeric:
        return True
    # Numeric sub_ids: group only when every sub-question is Selection-Based (MCQ).
    # This handles Q7.1/Q7.2/… patterns where all items belong to one question stem.
    all_mcq = all(sq.get("answer_type", "") == "Selection-Based" for sq in sub_qs)
    return all_mcq


def _dominant_ui_type(sub_qs: list[dict]) -> str:
    """Return the most common UI type among a list of sub-questions."""
    from collections import Counter
    types = [
        _map_taxonomy_to_ui_type(sq.get("answer_type", ""), sq.get("answer_subtype", ""))
        for sq in sub_qs
    ]
    return Counter(types).most_common(1)[0][0] if types else "recall_general"


def _serialize_grouped_model_answer(sub_qs: list[dict], parent_ui_type: str) -> str:
    """
    Build the {"__sub__":[...]} JSON string the UI uses for grouped cards.

    Each entry:
      label        — sub_id letter ("a", "b", …)
      question_text — the sub-question text
      answer_key   — serialized answer (plain text, or MC/TF JSON if parent_ui_type matches)
      sub_q_img    — empty (no image from extraction)
      sub_ans_img  — empty
    """
    sub_list = []
    for sq in sub_qs:
        label = sq.get("sub_id", "a")
        q_text = (sq.get("sub_question_text") or "").strip()
        answer_key = _serialize_model_answer(sq, parent_ui_type)
        sub_list.append({
            "label":         label,
            "question_text": q_text,
            "answer_key":    answer_key,
            "sub_q_img":     "",
            "sub_ans_img":   "",
        })
    return json.dumps({"__sub__": sub_list}, ensure_ascii=False)


# ── Post-extraction diagram image attachment helpers ────────────────────────────
#
# These helpers run AFTER the supervisor serializers (_serialize_model_answer,
# _serialize_grouped_model_answer) and inject the answer-key image path into
# the stored model_answer field.
#
# The grading engine's existing _extract_answer_key_images() already handles
# two formats without any modification:
#   Case 1 (used here): JSON with an "image_path" key
#                       → {"text": "...", "image_path": "path/to/img.png"}
#   Case 2 (legacy):    a plain string ending in an image extension
#
# By embedding the path in Case-1 format we enable multimodal grading for
# diagram questions without touching the grading pipeline at all.

_DIAGRAM_ANSWER_TYPES = frozenset({
    "diagrammatic_aoa",
    "diagrammatic_general",
})


def _embed_image_in_flat_answer(plain_answer: str, image_path: str) -> str:
    """
    Wrap a flat (non-MCQ, non-TF) answer in a JSON object so the grading
    engine's _extract_answer_key_images() picks up the image without
    any modification to the grading pipeline.

    Output: '{"text": "<plain>", "image_path": "<path>"}'
    """
    return json.dumps(
        {"text": plain_answer, "image_path": image_path},
        ensure_ascii=False,
    )


def _embed_images_in_grouped_answer(grouped_ma: str, sub_qs: list[dict]) -> str:
    """
    Post-process a {"__sub__": [...]} JSON string to embed answer_key_image_path
    into each sub-entry's answer_key field so the grading engine can locate the
    diagram image for each sub-question without modification.

    Only entries that have a corresponding has_drawing sub-question are modified.
    Sub-entries whose sub_question has no image are left unchanged.
    """
    try:
        parsed = json.loads(grouped_ma)
    except (json.JSONDecodeError, ValueError):
        return grouped_ma

    img_by_sub_id: dict[str, str] = {
        sq.get("sub_id", "a"): (sq.get("answer_key_image_path") or "").strip()
        for sq in sub_qs
    }

    changed = False
    for entry in parsed.get("__sub__", []):
        img_path = img_by_sub_id.get(entry.get("label", "a"), "")
        if img_path:
            entry["answer_key"] = json.dumps(
                {"text": entry.get("answer_key", ""), "image_path": img_path},
                ensure_ascii=False,
            )
            changed = True

    return json.dumps(parsed, ensure_ascii=False) if changed else grouped_ma


def _extract_answer_text(model_answer: str) -> str:
    """
    Extract plain text from a model_answer that may be diagram JSON.

    When model_answer is '{"text": "...", "image_path": "..."}' (as written
    by _embed_image_in_flat_answer), return just the text part so that
    _answer_key_from_db() produces a readable correct_answer for student
    extraction rather than raw JSON.

    For all other formats (plain text, MCQ JSON, TF JSON, __sub__ JSON) the
    value is returned unchanged.
    """
    if not model_answer:
        return model_answer
    stripped = model_answer.strip()
    if not stripped.startswith("{"):
        return model_answer
    # Only unwrap the diagram-image wrapper format; leave MCQ/TF/sub JSON intact
    try:
        parsed = json.loads(stripped)
        if (
            isinstance(parsed, dict)
            and "text" in parsed
            and "image_path" in parsed
            and "type" not in parsed       # not MCQ/TF JSON
            and "__sub__" not in parsed    # not grouped-card JSON
        ):
            return parsed.get("text", "")
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    return model_answer


# ── Shared helpers ──────────────────────────────────────────────────────────────

def _get_exam_or_404(exam_id: int, db: Session) -> Exam:
    exam = db.get(Exam, exam_id)
    if exam is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Exam {exam_id} not found.",
        )
    return exam


def _require_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    stripped_differs = key != os.environ.get("ANTHROPIC_API_KEY", "")
    print(
        f"[answer-key-extract] API key diagnostic: "
        f"present={bool(key)} length={len(key)} "
        f"first8={key[:8]!r} last4={key[-4:]!r} "
        f"strip_changed={stripped_differs}"
    )
    if not key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ANTHROPIC_API_KEY is not configured on the server.",
        )
    return key


# ── Build answer_key dict from DB (for student extraction) ──────────────────────

def _answer_key_from_db(exam_id: int, db: Session) -> dict:
    """
    Reconstruct the answer_key_structured dict from exam_model_answers rows.
    This is the format expected by student_extractor._build_key_lookup().

    Grouped rows (model_answer starts with {"__sub__":}) are expanded into
    proper sub_questions so every sub-question (Q1a, Q1b, Q1c…) gets its own
    lookup key.  Without this expansion only the first sub-question would match,
    and all remaining sub-questions would be marked unmatched_question=True.
    """
    rows = (
        db.query(ExamModelAnswer)
        .filter(ExamModelAnswer.exam_id == exam_id)
        .order_by(ExamModelAnswer.question_number)
        .all()
    )
    questions = []
    for row in rows:
        ma = row.model_answer or ""
        qnum_label = f"Q{row.question_number}"

        # Grouped row: model_answer is {"__sub__": [{label, question_text, answer_key}, ...]}
        # Expand into one sub_question per label so the key lookup generates Q1A, Q1B, etc.
        if ma.startswith('{"__sub__":'):
            try:
                parsed = json.loads(ma)
                sub_entries = parsed.get("__sub__", [])
            except (json.JSONDecodeError, ValueError):
                sub_entries = []

            if sub_entries:
                sub_questions = [
                    {
                        "sub_id":             entry.get("label", "a"),
                        "sub_question_text":  entry.get("question_text", ""),
                        "answer_type":        row.answer_type or "",
                        "answer_subtype":     "",
                        # Strip the diagram image-wrapper JSON so the student extractor
                        # receives plain text as the reference answer, not raw JSON.
                        "correct_answer":     _extract_answer_text(entry.get("answer_key", "")),
                        "mcq_correct_option": None,
                    }
                    for entry in sub_entries
                ]
                questions.append({
                    "outcome_number":  1,
                    "question_number": qnum_label,
                    "question_text":   row.question_text,
                    "sub_questions":   sub_questions,
                })
                continue

        # Flat row: single sub-question; sub_id "a" keeps has_multiple=False
        # so the lookup key equals the question number itself (e.g. "Q2").
        questions.append({
            "outcome_number":  1,
            "question_number": qnum_label,
            "question_text":   row.question_text,
            "sub_questions": [{
                "sub_id":             "a",
                "sub_question_text":  row.question_text,
                "answer_type":        row.answer_type or "",
                "answer_subtype":     "",
                # Strip diagram image-wrapper JSON so student extractor sees plain text.
                "correct_answer":     _extract_answer_text(ma),
                "mcq_correct_option": None,
            }],
        })
    return {
        "exam_info": {},
        "outcomes": [{
            "outcome_number": 1,
            "description":    None,
            "questions":      questions,
        }],
    }


# ══════════════════════════════════════════════════════════════════════════════
#  Answer Key Extraction
# ══════════════════════════════════════════════════════════════════════════════

def _do_answer_key_extraction(
    exam_id: int,
    file_path: str,
    api_key: str,
    db_url: str,
) -> None:
    """
    Background task: run answer key extraction, then upsert results into
    exam_model_answers (score = 0 placeholder; marks remain manual).
    """
    from database import SessionLocal

    output_dir = str(_AK_UPLOAD_DIR / str(exam_id) / "output")
    print(f"[answer-key-extraction-start] exam_id={exam_id} file_path={file_path}")
    try:
        answer_key = run_answer_key_extraction(
            file_path=file_path,
            api_key=api_key,
            output_dir=output_dir,
        )
    except Exception as exc:
        # Log to stderr; no status table — caller sees empty model answers
        import traceback
        print(f"[answer-key-extract] FAILED during extraction for exam_id={exam_id}: {exc}")
        traceback.print_exc()
        return

    db = SessionLocal()
    try:
        print(f"[answer-key-extract] writing extracted answers to DB for exam_id={exam_id}")
        # Delete all existing model answers for this exam before re-inserting
        db.query(ExamModelAnswer).filter(ExamModelAnswer.exam_id == exam_id).delete()
        db.flush()

        seq = 1
        for outcome in answer_key.get("outcomes", []):
            for q in _regroup_by_qnum(outcome.get("questions", [])):
                sub_qs = q.get("sub_questions", [])
                if not sub_qs:
                    continue

                qnum_label = q.get("question_number", "Q?")
                if _should_group(sub_qs):
                    # ── Grouped card: one DB row with {"__sub__":[...]} payload ──
                    parent_ui_type = _dominant_ui_type(sub_qs)
                    sub_ids = [s.get("sub_id", "?") for s in sub_qs]
                    print(
                        f"[answer-key-extract] GROUPED seq={seq} "
                        f"parent={qnum_label} sub_count={len(sub_qs)} sub_ids={sub_ids} "
                        f"type={parent_ui_type}"
                    )

                    # Collect answer_key_image_path from every sub-question in the group.
                    # (For AOA questions the whole page image is attached to each sub-question
                    # that has has_drawing=True; deduplicate so the same path isn't stored twice.)
                    _aki_paths_seen: set[str] = set()
                    _aki_paths: list[str] = []
                    for _sq in sub_qs:
                        _p = (_sq.get("answer_key_image_path") or "").strip()
                        if _p and _p not in _aki_paths_seen:
                            _aki_paths.append(_p)
                            _aki_paths_seen.add(_p)
                    _aki_json = json.dumps(_aki_paths) if _aki_paths else None

                    # AOA safety check — grouped card
                    if parent_ui_type == "diagrammatic_aoa" and not _aki_paths:
                        print(
                            f"[diagram-debug] missing_answer_key_image=True "
                            f"question={qnum_label} type={parent_ui_type}"
                        )

                    # Build the grouped model_answer, then inject image paths for
                    # diagram sub-entries so the grading engine finds them via
                    # the existing _extract_answer_key_images() mechanism.
                    _grouped_ma = _serialize_grouped_model_answer(sub_qs, parent_ui_type)
                    if parent_ui_type in _DIAGRAM_ANSWER_TYPES and _aki_paths:
                        _grouped_ma = _embed_images_in_grouped_answer(_grouped_ma, sub_qs)

                    db.add(ExamModelAnswer(
                        exam_id           = exam_id,
                        question_number   = seq,
                        question_text     = (q.get("question_text") or "").strip(),
                        model_answer      = _grouped_ma,
                        score             = 0,   # placeholder — manual in UI
                        answer_type       = parent_ui_type,
                        answer_key_images = _aki_json,
                    ))
                    seq += 1
                else:
                    # ── Flat cards: one DB row per sub-question ────────────────
                    has_multiple = len(sub_qs) > 1   # numeric MCQ series
                    print(
                        f"[answer-key-extract] FLAT seq_start={seq} "
                        f"qnum={qnum_label} sub_count={len(sub_qs)} "
                        f"has_multiple={has_multiple}"
                    )
                    for sq in sub_qs:
                        sid     = sq.get("sub_id", "a")
                        prefix  = f"{qnum_label}{sid}: " if has_multiple else ""
                        ui_type = _map_taxonomy_to_ui_type(
                            sq.get("answer_type", ""),
                            sq.get("answer_subtype", ""),
                        )

                        # Attach answer key image path for this sub-question (if any)
                        _aki_path = (sq.get("answer_key_image_path") or "").strip()
                        _aki_json = json.dumps([_aki_path]) if _aki_path else None

                        # AOA safety check — flat card
                        if ui_type == "diagrammatic_aoa" and not _aki_path:
                            print(
                                f"[diagram-debug] missing_answer_key_image=True "
                                f"question={qnum_label}{sid} type={ui_type}"
                            )

                        # Serialize the answer, then embed image path for diagram
                        # types so the grading engine's _extract_answer_key_images()
                        # finds it without any modification to the grading pipeline.
                        _plain_ma = _serialize_model_answer(sq, ui_type)
                        if ui_type in _DIAGRAM_ANSWER_TYPES and _aki_path:
                            _stored_ma = _embed_image_in_flat_answer(_plain_ma, _aki_path)
                        else:
                            _stored_ma = _plain_ma

                        db.add(ExamModelAnswer(
                            exam_id           = exam_id,
                            question_number   = seq,
                            question_text     = prefix + (sq.get("sub_question_text") or "").strip(),
                            model_answer      = _stored_ma,
                            score             = 0,   # placeholder — manual in UI
                            answer_type       = ui_type,
                            answer_key_images = _aki_json,
                        ))
                        seq += 1

        # Advance workflow_step if needed
        exam = db.get(Exam, exam_id)
        if exam and exam.workflow_step < 3:
            exam.workflow_step = 3

        db.commit()
        print(f"[answer-key-extract] DB write complete, {seq - 1} questions inserted for exam_id={exam_id}")
    except Exception as exc:
        db.rollback()
        import traceback
        print(f"[answer-key-extract] FAILED during DB write for exam_id={exam_id}: {exc}")
        traceback.print_exc()
    finally:
        db.close()


@router.post(
    "/answer-key/{exam_id}",
    response_model=AnswerKeyExtractionResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_202_ACCEPTED,
)
async def extract_answer_key(
    exam_id:          int,
    background_tasks: BackgroundTasks,
    file:             UploadFile       = File(...),
    current_user:     User             = Depends(get_current_user),
    db:               Session          = Depends(get_db),
):
    """
    Upload an answer key PDF or DOCX and trigger background extraction.

    The extraction populates exam_model_answers with the extracted questions
    and correct answers.  Score (marks) fields are set to 0 as a placeholder
    and must be filled in manually via the UI.

    Returns HTTP 202 immediately; extraction runs in the background.
    Poll GET /model-answers?exam_id={exam_id} until rows appear.
    """
    print(f"[answer-key-extract] request received exam_id={exam_id} filename={file.filename}")
    exam = _get_exam_or_404(exam_id, db)
    require_exam_access(exam, current_user)
    api_key = _require_api_key()
    print(f"[answer-key-extract] API key present, length={len(api_key)}")

    # Validate file type
    filename = file.filename or ""
    if not filename.lower().endswith((".pdf", ".docx")):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Answer key must be a PDF or DOCX file.",
        )

    # Save uploaded file to disk
    upload_dir = _AK_UPLOAD_DIR / str(exam_id)
    upload_dir.mkdir(parents=True, exist_ok=True)
    dest = upload_dir / filename
    counter = 1
    while dest.exists():
        stem = Path(filename).stem
        dest = upload_dir / f"{stem}_{counter}{Path(filename).suffix}"
        counter += 1

    raw = await file.read()
    print(f"[answer-key-extract] saving uploaded file to {dest} ({len(raw)} bytes)")
    dest.write_bytes(raw)
    print(f"[answer-key-extract] file saved successfully")
    print(f"[answer-key-upload] exam_id={exam_id} file_path={dest} file_size={len(raw)}")

    # ── Clear stale model answers NOW, before queuing the background task.
    # This prevents the frontend poll from finding old rows and stopping early
    # before the new extraction has finished writing its grouped results.
    old_count = db.query(ExamModelAnswer).filter(ExamModelAnswer.exam_id == exam_id).delete()
    db.commit()
    print(f"[answer-key-extract] cleared {old_count} stale model-answer rows for exam_id={exam_id}")

    # Queue background extraction
    print(f"[answer-key-extract] queuing background extraction task for exam_id={exam_id}")
    background_tasks.add_task(
        _do_answer_key_extraction,
        exam_id=exam_id,
        file_path=str(dest),
        api_key=api_key,
        db_url="",  # SessionLocal() uses module-level engine; db_url unused
    )

    return {"message": "Answer key extraction started.", "questionsQueued": 0}


# ══════════════════════════════════════════════════════════════════════════════
#  Student Answer Extraction
# ══════════════════════════════════════════════════════════════════════════════

def _mark_answer_file_failed(af_id: int | None, error: str) -> None:
    """
    Mark an AnswerFile row as failed with a reason string.
    Opens its own session; safe to call from a background thread.
    No-op when af_id is None.
    """
    if af_id is None:
        return
    from database import SessionLocal
    db = SessionLocal()
    try:
        af = db.get(AnswerFile, af_id)
        if af is not None:
            af.extraction_status = ExtractionStatus.failed
            af.extraction_error  = error[:1000]   # cap to avoid truncation issues
            db.commit()
            print(f"[ANSWER-EXTRACTION-FAILED] answer_file_id={af_id} error={error[:120]!r}")
    except Exception as exc:
        db.rollback()
        print(f"[ANSWER-EXTRACTION-FAILED] could not write failed status: {exc}")
    finally:
        db.close()


def _do_student_extraction(
    exam_id: int,
    file_paths: list[str],
    answer_key: dict,
    api_key: str,
    answer_file_id_map: dict[str, int],   # stem → answer_file.id
) -> None:
    """
    Background task: run student extraction, store rows in
    student_extractions + student_answer_items.

    Marks policy: examiner_marks_numeric, examiner_marks_raw, expected_marks,
    outcome_marks, total_marks are NEVER written to the DB.
    """
    from database import SessionLocal

    output_dir = str(_EXTR_OUTPUT_DIR / str(exam_id) / "output")

    # ── Build Answer Key type lookup (uppercase question_id → UI answer_type) ──
    # This is the authoritative source of truth: when the Answer Key defines a
    # type for a question, it ALWAYS wins over whatever the extractor detected.
    # Falls back to extractor type only when no Answer Key entry exists.
    _ak_type_lookup: dict[str, str] = {}
    for outcome in answer_key.get("outcomes", []):
        for q_entry in outcome.get("questions", []):
            qnum = q_entry.get("question_number", "")
            sub_qs = q_entry.get("sub_questions", [])
            has_multiple = len(sub_qs) > 1 or (
                sub_qs and sub_qs[0].get("sub_id", "a") != "a"
            )
            for sq in sub_qs:
                sid = sq.get("sub_id", "a")
                qid = f"{qnum}{sid}" if has_multiple else qnum
                ak_type = sq.get("answer_type", "") or ""
                if ak_type:
                    _ak_type_lookup[qid.upper()] = ak_type

    def _on_student_done(stem: str, result: dict) -> None:
        """
        Persist one student's extraction to the DB immediately.

        Uses upsert logic: if a row already exists for this (exam_id, student_id_num),
        update it and replace its answer items.  This prevents duplicate rows when
        extraction is triggered more than once for the same student.
        """
        db = SessionLocal()
        try:
            sinfo          = result.get("student_info", {})
            student_id_num = sinfo.get("student_id") or None

            # ── Resolve answer_file_id from stem ──────────────────────────────
            resolved_afid = answer_file_id_map.get(stem)
            if resolved_afid is None:
                # Stem didn't match — log all known stems so the mismatch is visible
                print(
                    f"[ANSWER-EXTRACTION-STEM-MISMATCH] stem={stem!r} "
                    f"known_stems={list(answer_file_id_map.keys())} "
                    f"exam_id={exam_id} — answer_file_id will be NULL"
                )
            print(
                f"[student-extraction-start] exam_id={exam_id} "
                f"stem={stem!r} student_id={student_id_num!r} "
                f"answer_file_id={resolved_afid}"
            )

            # ── Upsert: check for an existing row before inserting ───────────
            existing = None
            if student_id_num:
                existing = (
                    db.query(StudentExtraction)
                    .filter(
                        StudentExtraction.exam_id       == exam_id,
                        StudentExtraction.student_id_num == student_id_num,
                    )
                    .first()
                )

            if existing:
                # ── answer_file_id preservation rule ─────────────────────────
                # Never overwrite an existing valid answer_file_id with a link
                # to a DIFFERENT file.  This prevents a student appearing in
                # multiple files from breaking the link on the already-extracted
                # file and making it revert to "Awaiting extraction".
                #
                # Only update answer_file_id when:
                #   a) it is currently NULL  → repair / first link
                #   b) it equals resolved_afid → same file, no-op
                if existing.answer_file_id is None:
                    existing.answer_file_id = resolved_afid
                elif existing.answer_file_id != resolved_afid:
                    print(
                        f"[ANSWER-EXTRACTION-SKIP-AFID] student_id_num={student_id_num!r} "
                        f"existing.answer_file_id={existing.answer_file_id} "
                        f"resolved_afid={resolved_afid} — preserving existing link"
                    )
                    # answer_file_id intentionally NOT updated — old link preserved

                existing.student_name     = sinfo.get("student_name")
                existing.student_section  = sinfo.get("section")
                existing.output_json_path = str(
                    Path(output_dir) / f"{stem}_extracted.json"
                )
                db.query(StudentAnswerItem).filter(
                    StudentAnswerItem.extraction_id == existing.id
                ).delete(synchronize_session=False)
                db.flush()
                extraction = existing
                print(
                    f"[extraction] updated existing extraction_id={existing.id} "
                    f"for student_id_num={student_id_num!r} exam_id={exam_id}"
                )
            else:
                extraction = StudentExtraction(
                    exam_id          = exam_id,
                    answer_file_id   = resolved_afid,
                    student_name     = sinfo.get("student_name"),
                    student_id_num   = student_id_num,
                    student_section  = sinfo.get("section"),
                    output_json_path = str(
                        Path(output_dir) / f"{stem}_extracted.json"
                    ),
                )
                db.add(extraction)
                db.flush()

            for page_data in result.get("pages", []):
                if "questions_on_page" not in page_data:
                    continue
                for q in page_data["questions_on_page"]:
                    # Serialise mcq_option_marks dict → JSON string for storage
                    mcq_marks_raw = q.get("mcq_option_marks")
                    mcq_marks_str = (
                        json.dumps(mcq_marks_raw, ensure_ascii=False)
                        if isinstance(mcq_marks_raw, dict) else None
                    )
                    # drawing_file_paths → take the first path only
                    drawing_paths = q.get("drawing_file_paths") or []
                    primary_drawing = drawing_paths[0] if drawing_paths else None

                    # ── Answer Key type override ───────────────────────────────
                    # If the Answer Key defines a type for this question_id, use
                    # it unconditionally (Answer Key is the source of truth).
                    # Fall back to extractor-stamped type only when no Answer Key
                    # entry exists for this question (e.g. unrecognised question).
                    qid_upper = (q.get("question_id") or "").upper()
                    resolved_type = (
                        _ak_type_lookup.get(qid_upper)
                        or q.get("answer_type")
                        or None
                    )

                    db.add(StudentAnswerItem(
                        extraction_id           = extraction.id,
                        question_id             = q.get("question_id") or "",
                        outcome_number          = q.get("outcome_number"),
                        student_answer          = q.get("student_answer"),
                        student_selected_option = q.get("student_selected_option"),
                        mcq_option_marks        = mcq_marks_str,
                        has_drawing             = bool(q.get("has_drawing")),
                        drawing_description     = q.get("drawing_description"),
                        drawing_file_path       = primary_drawing,
                        answer_type             = resolved_type,
                        answer_subtype          = q.get("answer_subtype") or None,
                        correct_answer          = q.get("correct_answer") or None,
                        mcq_key_correct_option  = q.get("mcq_key_correct_option"),
                        # examiner_marks_numeric, examiner_marks_raw,
                        # expected_marks — intentionally NOT stored
                    ))

            answers_detected = sum(
                len(page.get("questions_on_page", []))
                for page in result.get("pages", [])
            )
            print(f"[student-extraction-complete] student_id={student_id_num!r} answers_detected={answers_detected}")

            # Mark the source AnswerFile as extracted so the UI can reflect status
            # without polling the student_extractions table separately.
            if resolved_afid is not None:
                af = db.get(AnswerFile, resolved_afid)
                if af is not None:
                    af.extraction_status = ExtractionStatus.extracted
                    af.extraction_error  = None

            db.commit()
            print(
                f"[ANSWER-EXTRACTION-FILE] file={stem} status=success "
                f"student_id={student_id_num!r}"
            )
        except Exception as exc:
            db.rollback()
            import traceback
            print(f"[extraction] DB write failed for student {stem}: {exc}")
            traceback.print_exc()
        finally:
            db.close()

    # Process files one-at-a-time so a single failure doesn't abort all extraction.
    # Each file gets its own try/except; errors are logged with full traceback.
    for fp in file_paths:
        stem   = Path(fp).stem
        af_id  = answer_file_id_map.get(stem)   # may be None if stem didn't match
        print(f"[ANSWER-EXTRACTION-FILE] file={Path(fp).name} stem={stem!r} status=starting")
        try:
            if not Path(fp).exists():
                err_msg = f"file_not_found: {fp!r}"
                print(
                    f"[ANSWER-EXTRACTION-TASK-ERROR] file={Path(fp).name} "
                    f"error={err_msg}"
                )
                _mark_answer_file_failed(af_id, err_msg)
                continue
            run_student_extraction(
                file_paths=[fp],
                answer_key=answer_key,
                api_key=api_key,
                output_dir=output_dir,
                on_student_done=_on_student_done,
            )
        except Exception as exc:
            import traceback as _tb
            print(
                f"[ANSWER-EXTRACTION-TASK-ERROR] file={Path(fp).name} "
                f"stem={stem!r} error={exc}"
            )
            _tb.print_exc()
            _mark_answer_file_failed(af_id, str(exc))


@router.post(
    "/students/{exam_id}",
    status_code=status.HTTP_202_ACCEPTED,
)
def trigger_student_extraction(
    exam_id:          int,
    background_tasks: BackgroundTasks,
    current_user:     User    = Depends(get_current_user),
    db:               Session = Depends(get_db),
):
    """
    Trigger background extraction for all uploaded answer files for an exam.

    Uses the exam's existing answer key (from exam_model_answers) to stamp
    answer types and correct answers onto each student's transcription.

    Returns HTTP 202 immediately.
    Poll GET /extraction/students/{exam_id} until rows appear.
    """
    exam = _get_exam_or_404(exam_id, db)
    require_exam_access(exam, current_user)
    api_key = _require_api_key()

    # Collect uploaded answer files
    answer_files = (
        db.query(AnswerFile)
        .filter(AnswerFile.exam_id == exam_id)
        .order_by(AnswerFile.uploaded_at.asc())
        .all()
    )
    if not answer_files:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No answer files have been uploaded for this exam.",
        )

    # Require at least one model answer so extraction has a key to work with
    has_key = (
        db.query(ExamModelAnswer)
        .filter(ExamModelAnswer.exam_id == exam_id)
        .first()
    ) is not None
    if not has_key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "No answer key found for this exam. "
                "Upload and extract an answer key first, or add questions manually."
            ),
        )

    # ── Determine which files still need extraction ───────────────────────────
    # An answer file is "already extracted" if there is a StudentExtraction row
    # that references its id via answer_file_id.  Files without a linked row
    # (or with a NULL answer_file_id extraction) are treated as pending.
    extracted_file_ids: set[int] = {
        row[0]
        for row in db.query(StudentExtraction.answer_file_id)
        .filter(
            StudentExtraction.exam_id == exam_id,
            StudentExtraction.answer_file_id.isnot(None),
        )
        .all()
    }

    already_extracted = [af for af in answer_files if af.id in extracted_file_ids]
    pending_files     = [af for af in answer_files if af.id not in extracted_file_ids]

    print(
        f"[ANSWER-EXTRACTION-SCOPE] exam_id={exam_id} "
        f"pending_files={len(pending_files)} already_extracted={len(already_extracted)}"
    )

    # Per-file diagnostic: show id, name, and extraction link for every file
    for af in answer_files:
        is_extracted = af.id in extracted_file_ids
        linked_se_id = next(
            (
                row[1]
                for row in db.query(StudentExtraction.answer_file_id, StudentExtraction.id)
                .filter(
                    StudentExtraction.exam_id == exam_id,
                    StudentExtraction.answer_file_id == af.id,
                )
                .all()
            ),
            None,
        )
        print(
            f"[ANSWER-EXTRACTION-FILE-STATUS] "
            f"answer_file.id={af.id} "
            f"filename={Path(af.file_path).name if af.file_path else '(no path)'} "
            f"is_extracted={is_extracted} "
            f"linked_student_extraction_id={linked_se_id}"
        )

    for af in already_extracted:
        print(
            f"[ANSWER-EXTRACTION-SKIP] file={Path(af.file_path).name} "
            f"answer_file_id={af.id} reason=already_extracted"
        )

    if not pending_files:
        return {
            "message": "All uploaded answer files have already been extracted.",
            "filesQueued": 0,
            "alreadyExtracted": len(already_extracted),
        }

    # Verify each pending file actually exists on disk before queuing
    existing_pending = []
    for af in pending_files:
        if not af.file_path:
            print(f"[ANSWER-EXTRACTION-SKIP] answer_file_id={af.id} reason=no_file_path")
            continue
        if not Path(af.file_path).exists():
            print(
                f"[ANSWER-EXTRACTION-SKIP] answer_file_id={af.id} "
                f"filename={Path(af.file_path).name} reason=file_not_on_disk "
                f"path={af.file_path!r}"
            )
            continue
        existing_pending.append(af)

    if not existing_pending:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Pending answer files are missing from disk. They may have been deleted.",
        )

    file_paths = [af.file_path for af in existing_pending]

    # Build stem → answer_file.id map for FK assignment (pending files only)
    af_id_map = {Path(af.file_path).stem: af.id for af in existing_pending}

    print(
        f"[ANSWER-EXTRACTION-TASK-START] exam_id={exam_id} files={len(file_paths)} "
        f"stem_to_id_map={af_id_map}"
    )
    for af in existing_pending:
        print(
            f"[ANSWER-EXTRACTION-FILE] file={Path(af.file_path).name} "
            f"answer_file_id={af.id} stem={Path(af.file_path).stem!r} status=queued"
        )

    # Reconstruct answer key from DB
    answer_key = _answer_key_from_db(exam_id, db)

    background_tasks.add_task(
        _do_student_extraction,
        exam_id=exam_id,
        file_paths=file_paths,
        answer_key=answer_key,
        api_key=api_key,
        answer_file_id_map=af_id_map,
    )

    return {
        "message": "Student extraction started.",
        "filesQueued": len(existing_pending),
        "alreadyExtracted": len(already_extracted),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  Read endpoints
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/students/{exam_id}",
    response_model=List[StudentExtractionRead],
    response_model_by_alias=True,
)
def list_student_extractions(
    exam_id:      int,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Return all StudentExtraction records for an exam, ordered by extraction time.
    Returns an empty list while extraction is still running.
    """
    exam = _get_exam_or_404(exam_id, db)
    require_exam_access(exam, current_user)
    rows = (
        db.query(StudentExtraction)
        .filter(StudentExtraction.exam_id == exam_id)
        .order_by(StudentExtraction.extracted_at.asc())
        .all()
    )
    return [StudentExtractionRead.from_orm(r) for r in rows]


@router.get(
    "/students/{exam_id}/{extraction_id}/answers",
    response_model=StudentExtractionWithAnswers,
    response_model_by_alias=True,
)
def get_student_extraction_answers(
    exam_id:       int,
    extraction_id: int,
    current_user:  User    = Depends(get_current_user),
    db:            Session = Depends(get_db),
):
    """Return a StudentExtraction record with all its answer items."""
    exam = _get_exam_or_404(exam_id, db)
    require_exam_access(exam, current_user)

    extraction = db.get(StudentExtraction, extraction_id)
    if extraction is None or extraction.exam_id != exam_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Extraction {extraction_id} not found for exam {exam_id}.",
        )

    answers = (
        db.query(StudentAnswerItem)
        .filter(StudentAnswerItem.extraction_id == extraction_id)
        .order_by(StudentAnswerItem.id.asc())
        .all()
    )

    return StudentExtractionWithAnswers(
        extraction=StudentExtractionRead.from_orm(extraction),
        answers=[StudentAnswerItemRead.from_orm(a) for a in answers],
    )


# ── PATCH /extraction/answers/{item_id} ───────────────────────────────────────

@router.patch(
    "/answers/{item_id}",
    response_model=StudentAnswerItemRead,
    summary="Update the student_answer text of a single answer item",
)
def patch_answer_item(
    item_id:      int,
    body:         StudentAnswerItemUpdate,
    db:           Session = Depends(get_db),
    current_user: User    = Depends(get_current_user),
):
    """
    Partially update a StudentAnswerItem.
    Access is verified through item → extraction → exam ownership.

    Open-ended answers: send { studentAnswer: "..." }.
    MCQ / True-False:   send { studentSelectedOption: "b", mcqOptionMarks: "{...}" }.
    Any combination of the three fields is accepted; omitted fields are left unchanged.
    At least one field must be supplied.
    """
    item = db.get(StudentAnswerItem, item_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Answer item not found.")

    extraction = db.get(StudentExtraction, item.extraction_id)
    if extraction is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Extraction not found.")

    exam = _get_exam_or_404(extraction.exam_id, db)
    require_exam_access(exam, current_user)

    if (body.student_answer is None
            and body.student_selected_option is None
            and body.mcq_option_marks is None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one field must be provided (studentAnswer, studentSelectedOption, or mcqOptionMarks).",
        )

    if body.student_answer is not None:
        item.student_answer = body.student_answer
    if body.student_selected_option is not None:
        item.student_selected_option = body.student_selected_option
    if body.mcq_option_marks is not None:
        item.mcq_option_marks = body.mcq_option_marks

    db.commit()
    db.refresh(item)
    return StudentAnswerItemRead.from_orm(item)


# ── DELETE /extraction/answers/{item_id} ──────────────────────────────────────

@router.delete(
    "/answers/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a single student answer item",
)
def delete_answer_item(
    item_id:      int,
    db:           Session = Depends(get_db),
    current_user: User    = Depends(get_current_user),
):
    """
    Delete a StudentAnswerItem permanently.
    Access is verified through item → extraction → exam ownership.
    """
    item = db.get(StudentAnswerItem, item_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Answer item not found.")

    extraction = db.get(StudentExtraction, item.extraction_id)
    if extraction is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Extraction not found.")

    exam = _get_exam_or_404(extraction.exam_id, db)
    require_exam_access(exam, current_user)

    db.delete(item)
    db.commit()
