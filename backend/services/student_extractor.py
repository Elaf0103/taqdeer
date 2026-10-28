"""
Student Answer Extraction Runner
=================================
Server-side equivalent of the student-extraction cells in
grading_notebook/exam_grading_system_generic.ipynb.

The notebook is the protected source — this file is an external adapter that
implements the same logic without any Colab dependencies.

Colab APIs replaced:
  files.upload()       → caller passes a list of file_paths
  userdata.get(key)    → os.environ["ANTHROPIC_API_KEY"]
  files.download()     → outputs written to output_dir; caller reads them

Marks policy (STRICT — do not change):
  The notebook may transcribe examiner marks from red ink on exam papers.
  This runner ignores ALL marks fields:
    - examiner_marks_numeric
    - examiner_marks_raw
    - expected_marks  (derived from answer key marks)
    - outcome_marks   (cover-page marks summary)
    - total_marks     (cover-page total)
  None of these fields are written to the DB or returned to the caller.
  Marks remain manual in the UI.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import time
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF
from anthropic import Anthropic, APIConnectionError, APIStatusError, RateLimitError
from PIL import Image


# ── Internal helpers (shared with answer_key_runner, kept local for isolation) ──

_MODEL = "claude-sonnet-4-20250514"


def _make_client(api_key: str) -> Anthropic:
    return Anthropic(api_key=api_key)


def _call_claude_with_retry(client: Anthropic, max_retries: int = 6,
                             base_delay: float = 2.0, **kwargs):
    """Exponential-backoff wrapper — mirrors notebook's call_claude_with_retry."""
    for attempt in range(max_retries):
        try:
            return client.messages.create(**kwargs)
        except (APIStatusError, RateLimitError, APIConnectionError) as exc:
            status = getattr(exc, "status_code", None)
            retryable = isinstance(exc, (RateLimitError, APIConnectionError)) or (
                status is not None and (status == 529 or status == 429 or status >= 500)
            )
            if not retryable or attempt == max_retries - 1:
                raise
            delay = base_delay * (2 ** attempt)
            time.sleep(delay)
    raise RuntimeError("Exhausted retries")


def _repair_json(text: str) -> str:
    """Fix common LLM JSON mistakes — mirrors notebook's repair_json."""
    lines = text.split("\n")
    repaired = []
    for line in lines:
        m = re.match(r'^(\s*"[^"]+"\s*:\s*)"(.+?)"(.+)$', line)
        if m:
            prefix, value, after = m.group(1), m.group(2), m.group(3)
            after_stripped = after.strip()
            if after_stripped and after_stripped[0] not in [",", "}", "]"]:
                terminator = ""
                if after_stripped[-1] in [",", "}", "]"]:
                    terminator = after_stripped[-1]
                    after_stripped = after_stripped[:-1]
                extra = after_stripped.replace('"', "'").strip()
                line = f'{prefix}"{value} {extra}"{terminator}'
        repaired.append(line)
    text = "\n".join(repaired)
    text = re.sub(r",\s*([}\]])", r"\1", text)
    return text


def _pdf_to_images(pdf_path: Path, dpi: int = 200) -> list[Image.Image]:
    doc = fitz.open(str(pdf_path))
    images = []
    zoom = dpi / 72
    mat = fitz.Matrix(zoom, zoom)
    for page_num in range(len(doc)):
        pix = doc[page_num].get_pixmap(matrix=mat)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        images.append(img)
    doc.close()
    return images


def _image_to_base64(img: Image.Image, max_size: int = 1600) -> str:
    w, h = img.size
    if max(w, h) > max_size:
        ratio = max_size / max(w, h)
        img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


# ── Student info extraction (cover page) ────────────────────────────────────────

_COVER_PROMPT = """\
This is the cover page of a university exam paper. Extract the following information.
The student name may be in Arabic or English handwriting.

Return ONLY valid JSON (no markdown fences):
{
  "student_name": "...",
  "student_id": "...",
  "section": "..."
}

If a field is not readable, put null. Transcribe the name exactly as written.
Note: Do NOT extract any marks, scores, or outcome marks — those are not needed.
"""


def _extract_student_info(client: Anthropic, page_b64: str) -> dict:
    response = _call_claude_with_retry(
        client,
        model=_MODEL,
        max_tokens=300,
        temperature=0.1,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {
                "type": "base64", "media_type": "image/png", "data": page_b64,
            }},
            {"type": "text", "text": _COVER_PROMPT},
        ]}],
    )
    text = response.content[0].text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"student_name": None, "student_id": None, "section": None}


# ── Student answer extraction (per page) ────────────────────────────────────────

def _build_answer_prompt(page_number: int, exam_structure: dict) -> str:
    exam_info = exam_structure.get("exam_info", {}) or {}
    course_code = exam_info.get("course_code") or "this exam"
    course_name = exam_info.get("course_name") or ""
    course_label = f"{course_code} ({course_name})" if course_name else course_code

    expected_lines = []
    for outcome in exam_structure.get("outcomes", []):
        onum = outcome.get("outcome_number", "?")
        for q in outcome.get("questions", []):
            qnum = q.get("question_number", "Q?")
            sub_qs = q.get("sub_questions", [])
            has_multiple = len(sub_qs) > 1 or (
                sub_qs and sub_qs[0].get("sub_id", "a") != "a"
            )
            for sq in sub_qs:
                sid = sq.get("sub_id", "a")
                qid = f"{qnum}{sid}" if has_multiple else qnum
                qtext = (sq.get("sub_question_text") or "")[:90]
                atype = sq.get("answer_type", "")
                expected_lines.append(
                    f"  - {qid} [Outcome {onum}, {atype}]: {qtext}"
                )
    expected_block = "\n".join(expected_lines) if expected_lines else (
        "  (no answer key structure available)"
    )

    return f"""\
You are an expert at reading handwritten exam answers. This is page {page_number} of a student's exam paper for {course_label}.

The exam contains the following questions overall. They may appear on any page of the student's paper — use this list to match each answer you find to the correct question_id:

{expected_block}

INK COLOR RULE — read before anything else:
  Student answers are written in the STUDENT'S pen, almost always BLUE or BLACK.
  Examiner grading marks are in RED or PINK ink.
  Before interpreting any mark on the page, decide whose mark it is:
    - Blue/black pen → the student. Goes into student_answer or student_selected_option.
    - Red/pink pen → the examiner. Goes into examiner_comments only (for descriptive comments).
  Examiner ink NEVER indicates the student's selection, NEVER adds to the student's answer,
  and NEVER changes which option the student chose.

CRITICAL TRANSCRIPTION RULES:
1. Write the student answer EXACTLY as the student wrote it — every word in English or any
   other language (Arabic, etc.). Preserve scripts. Do NOT replace Arabic with "[Arabic text]".
2. Keep ALL misspellings, grammatical errors, and abbreviations exactly as written.
3. If text is unclear, include your best guess with [unclear] markers.

ONE ENTRY PER SUB-QUESTION — NEVER MERGE:
   Count the sub-question labels (a, b, c, d, e, f, 1, 2, 3, ...) printed ON THE PAGE and
   emit EXACTLY ONE JSON object per printed label. Each label owns its own student_answer.

   NEVER concatenate two sub-questions into one.
   If a sub-question's area is truly blank, still emit an entry with student_answer = null.

DO NOT GUESS THE ANSWER FROM QUESTION MEANING:
   Report what is physically on the page, not what the correct answer logically should be.

MCQ / SELECTION-BASED RULES:
   For each option, categorize into: SELECTED, CROSSED_OUT, or UNMARKED (based on student ink only).
   Fill "mcq_option_marks" dict: {{letter → "SELECTED" | "CROSSED_OUT" | "UNMARKED"}}.
   Set student_selected_option from SELECTED entries (null if none selected).
   Red/pink examiner marks never indicate the student's selection.

DRAWING RULES:
   If the student drew any diagram, set has_drawing = true and write a DETAILED
   drawing_description covering all nodes, edges, labels, and annotations.
   A description like "network diagram drawn" is NOT acceptable.

DO NOT classify the answer type — that will be stamped separately from the answer key.

CRITICAL JSON FORMATTING RULES:
- Return ONLY valid JSON. No text outside the JSON object.
- ALL string values must be fully enclosed in double quotes.
- If a field has no value, use null (without quotes).
- Do NOT use trailing commas.

Return ONLY valid JSON (no markdown fences):
{{
  "page_number": {page_number},
  "questions_on_page": [
    {{
      "question_id": "Q1a",
      "student_answer": "exact transcription preserved",
      "mcq_option_marks": null,
      "student_selected_option": null,
      "has_drawing": false,
      "drawing_description": null,
      "examiner_comments": null
    }}
  ]
}}

For MCQ questions, mcq_option_marks MUST be filled as a dict.
For non-MCQ questions, set mcq_option_marks to null.
Match each answer to the question_id from the expected list above."""


def _extract_answers_from_page(client: Anthropic, page_b64: str,
                                page_number: int, exam_structure: dict) -> dict:
    prompt = _build_answer_prompt(page_number, exam_structure)
    response = _call_claude_with_retry(
        client,
        model=_MODEL,
        max_tokens=4000,
        temperature=0.1,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {
                "type": "base64", "media_type": "image/png", "data": page_b64,
            }},
            {"type": "text", "text": prompt},
        ]}],
    )
    text = response.content[0].text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            return json.loads(_repair_json(text))
        except json.JSONDecodeError:
            # Ask Claude to fix the JSON
            try:
                fix_response = _call_claude_with_retry(
                    client,
                    model=_MODEL,
                    max_tokens=3000,
                    temperature=0.0,
                    messages=[{"role": "user", "content": (
                        "The following JSON is malformed. Fix it to be valid JSON. "
                        "Return ONLY the fixed JSON, no explanation, no markdown fences.\n\n"
                        + text
                    )}],
                )
                fixed = fix_response.content[0].text.strip()
                fixed = re.sub(r"^```(?:json)?\s*|\s*```$", "", fixed)
                return json.loads(fixed)
            except Exception:
                return {"page_number": page_number, "raw_response": text, "parse_error": True}


# ── Answer key lookup table ─────────────────────────────────────────────────────

def _build_key_lookup(answer_key: dict) -> dict[str, dict]:
    """Build a dict keyed by upper-case question_id for fast stamp lookups."""
    lookup: dict[str, dict] = {}
    for outcome in answer_key.get("outcomes", []):
        onum = outcome.get("outcome_number")
        for q in outcome.get("questions", []):
            qnum = q.get("question_number", "")
            sub_qs = q.get("sub_questions", [])
            has_multiple = len(sub_qs) > 1 or (
                sub_qs and sub_qs[0].get("sub_id", "a") != "a"
            )
            for sq in sub_qs:
                sid = sq.get("sub_id", "a")
                qid = f"{qnum}{sid}" if has_multiple else qnum
                lookup[qid.upper()] = {
                    "answer_type":            sq.get("answer_type", ""),
                    "answer_subtype":         sq.get("answer_subtype", ""),
                    "outcome_number":         onum,
                    "correct_answer":         sq.get("correct_answer", ""),
                    "mcq_key_correct_option": sq.get("mcq_correct_option"),
                    "answer_key_image_path":  sq.get("answer_key_image_path"),
                    # "expected_marks" intentionally excluded
                }
    return lookup


# ── Single student pipeline ─────────────────────────────────────────────────────

def _process_student(
    client: Anthropic,
    pdf_path: Path,
    answer_key: dict,
    key_lookup: dict[str, dict],
    drawings_dir: Path,
) -> dict:
    """
    Full pipeline for one student PDF.

    Returns a dict with keys:
      file_name, student_info (name/id/section only), pages, drawing_files
    """
    student_tag = pdf_path.stem
    page_images = _pdf_to_images(pdf_path, dpi=200)

    # Cover page → student identity only (marks fields stripped from prompt)
    cover_b64 = _image_to_base64(page_images[0])
    raw_info = _extract_student_info(client, cover_b64)
    student_info = {
        "student_name":    raw_info.get("student_name"),
        "student_id":      raw_info.get("student_id"),
        "section":         raw_info.get("section"),
        # outcome_marks and total_marks intentionally NOT included
    }

    # Subsequent pages → answer transcription
    all_answers: list[dict] = []
    for page_idx in range(1, len(page_images)):
        page_num = page_idx + 1
        page_b64 = _image_to_base64(page_images[page_idx])
        page_answers = _extract_answers_from_page(client, page_b64, page_num, answer_key)
        all_answers.append(page_answers)

    # Stamp answer_type / subtype / outcome / correct_answer from answer key
    for page_data in all_answers:
        if "questions_on_page" not in page_data:
            continue
        for q in page_data["questions_on_page"]:
            qid = (q.get("question_id") or "").upper()
            info = key_lookup.get(qid)
            if info:
                q["answer_type"]            = info["answer_type"]
                q["answer_subtype"]         = info["answer_subtype"]
                q["outcome_number"]         = info["outcome_number"]
                q["correct_answer"]         = info["correct_answer"]
                q["mcq_key_correct_option"] = info["mcq_key_correct_option"]
                q["answer_key_image_path"]  = info["answer_key_image_path"]
            else:
                q["answer_type"]            = ""
                q["answer_subtype"]         = ""
                q["outcome_number"]         = None
                q["correct_answer"]         = ""
                q["mcq_key_correct_option"] = None
                q["unmatched_question"]     = True
            # examiner_marks_numeric / examiner_marks_raw / expected_marks NOT added

    # Save drawing pages
    question_locations: list[tuple[int, dict]] = []
    for page_data in all_answers:
        if "questions_on_page" not in page_data:
            continue
        pnum = page_data.get("page_number")
        for q in page_data["questions_on_page"]:
            question_locations.append((pnum, q))

    drawing_paths: list[str] = []
    for i, (pnum, q) in enumerate(question_locations):
        is_drawing = (
            q.get("answer_type") == "Diagrammatic and Visual"
            or q.get("has_drawing") is True
        )
        if not is_drawing:
            continue

        qid = q.get("question_id", "unknown").replace(".", "_")
        next_q_page = None
        for (npnum, _) in question_locations[i + 1 :]:
            if npnum > pnum:
                next_q_page = npnum
                break
        last_page = (next_q_page - 1) if next_q_page else len(page_images)

        saved_for_q: list[str] = []
        for search_pn in range(pnum, last_page + 1):
            idx = search_pn - 1
            if not (0 <= idx < len(page_images)):
                continue
            fname = f"{student_tag}_{qid}_page{search_pn}.png"
            fpath = drawings_dir / fname
            page_images[idx].save(str(fpath), "PNG")
            saved_for_q.append(str(fpath))
            drawing_paths.append(str(fpath))

        if saved_for_q:
            q["drawing_file_paths"] = saved_for_q
            q["has_drawing"] = True

    return {
        "file_name":    pdf_path.name,
        "student_info": student_info,
        "pages":        all_answers,
        "drawing_files": drawing_paths,
    }


# ── Public entry point ──────────────────────────────────────────────────────────

def run_student_extraction(
    file_paths: list[str],
    answer_key: dict,
    api_key: str,
    output_dir: str,
    on_student_done: Optional[callable] = None,
) -> dict:
    """
    Extract student answers from a list of PDF file paths.

    Parameters
    ----------
    file_paths      : list of absolute paths to student answer PDFs
    answer_key      : the answer_key_structured dict (from answer_key_runner or DB)
    api_key         : Anthropic API key
    output_dir      : directory where per-student JSONs and drawings are written
    on_student_done : optional callback(stem: str, result: dict) called after each student

    Returns
    -------
    dict  —  all_results keyed by pdf stem:
      {
        "student_001": {
          "file_name":    "student_001.pdf",
          "student_info": {"student_name": "...", "student_id": "...", "section": "..."},
          "pages": [ { "page_number": 2, "questions_on_page": [ ... ] } ],
          "drawing_files": [ "uploads/extractions/5/output/drawings/..." ]
        },
        ...
      }

    Marks policy
    ------------
    examiner_marks_numeric, examiner_marks_raw, expected_marks, outcome_marks,
    total_marks are never written to output_dir or returned in the result dict.
    """
    client = _make_client(api_key)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    drawings_dir = out_dir / "drawings"
    drawings_dir.mkdir(parents=True, exist_ok=True)

    key_lookup = _build_key_lookup(answer_key)
    all_results: dict[str, dict] = {}

    for fp in file_paths:
        pdf_path = Path(fp)
        stem = pdf_path.stem
        out_file = out_dir / f"{stem}_extracted.json"

        # Skip already-processed files (resume support)
        if out_file.exists():
            with open(out_file, encoding="utf-8") as f:
                all_results[stem] = json.load(f)
            if on_student_done:
                on_student_done(stem, all_results[stem])
            continue

        result = _process_student(client, pdf_path, answer_key, key_lookup, drawings_dir)
        all_results[stem] = result

        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)

        if on_student_done:
            on_student_done(stem, result)

    # Write combined output
    combined_path = out_dir / "all_students_extracted.json"
    with open(combined_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False)

    return all_results
