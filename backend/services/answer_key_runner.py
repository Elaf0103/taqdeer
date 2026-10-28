"""
Answer Key Extraction Runner
============================
Server-side equivalent of the answer-key extraction cells in
grading_notebook/exam_grading_system_generic.ipynb.

The notebook is the protected source — this file is an external adapter that
implements the same logic without any Colab dependencies.

Colab APIs replaced:
  files.upload()       → caller passes file_path directly
  userdata.get(key)    → os.environ["ANTHROPIC_API_KEY"]
  files.download()     → outputs are written to output_dir; caller reads them

NOT implemented here:
  student extraction   → see student_extractor.py

Marks policy:
  The notebook may include marks/scores in its output JSON.
  This runner intentionally ignores all marks fields:
    - exam_info.total_marks
    - outcomes[].total_marks
    - sub_questions[].marks
  Marks remain manual in the UI.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF
from anthropic import Anthropic, APIConnectionError, APIStatusError, RateLimitError
from PIL import Image


# ── Taxonomy (mirrors notebook constant — kept in sync, not imported from it) ──

_TAXONOMY_PROMPT_TEXT = """\
Category: Selection-Based
  - Single MCQ
  - Multi-select MCQ
  - True/False
  - True/False with correction
  - Matching
  - Ordering
  - Fill-in-the-blank with word bank
Category: Recall and Short Answer
  - Single term answer
  - Definition
  - List/Enumerate
  - List with explanation
  - Differentiation/Comparison
  - Fill-in-the-blank (no bank)
  - Acronym expansion
  - Give an example
Category: Mathematical and Computational
  - Formula application
  - Multi-step calculation
  - Conversion
  - Scheduling calculations
Category: Code and Programming
  - Write code snippet
  - Complete missing code
  - Debug/fix code
Category: Diagrammatic and Visual
  - Flowchart
  - ER diagram
  - UML diagram
  - Network Diagram (AOA/AON)
Category: Table and Structured Data
  - Fill-in-the-table
  - Build a table from scratch
Category: Algorithm Execution and Tracing
  - Trace algorithm execution
  - Show data-structure state
Category: Analysis and Interpretation
  - Interpret output/results
  - Compare scenarios
Category: Design and Open-Ended
  - Design a system/schema
  - Propose a solution
Category: Essay and Explanation
  - Explain a concept
  - Discuss pros/cons
"""


# ── Internal helpers ────────────────────────────────────────────────────────────

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


def _docx_to_images(docx_path: Path, dpi: int = 200) -> list[Image.Image]:
    out_dir = docx_path.parent / "_docx_pdf_tmp"
    out_dir.mkdir(exist_ok=True)
    subprocess.run(
        ["libreoffice", "--headless", "--convert-to", "pdf",
         "--outdir", str(out_dir), str(docx_path)],
        check=True, capture_output=True,
    )
    pdf_path = out_dir / (docx_path.stem + ".pdf")
    return _pdf_to_images(pdf_path, dpi=dpi)


def _answer_key_to_images(path: Path, dpi: int = 200) -> list[Image.Image]:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return _pdf_to_images(path, dpi=dpi)
    elif ext == ".docx":
        return _docx_to_images(path, dpi=dpi)
    else:
        raise ValueError(f"Unsupported answer key format: {ext}. Use PDF or DOCX.")


def _image_to_base64(img: Image.Image, max_size: int = 1600) -> str:
    w, h = img.size
    if max(w, h) > max_size:
        ratio = max_size / max(w, h)
        img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


# ── Per-page extraction ─────────────────────────────────────────────────────────

_MODEL = "claude-sonnet-4-20250514"

_PAGE_PROMPT_TEMPLATE = """\
You are extracting an official EXAM ANSWER KEY. This is page {page_number} of {total_pages}.

The answer key contains BOTH the question text AND the correct answers (and marks per question).
Extract everything on this page exactly as written.

ANSWER TYPE TAXONOMY — classify each sub-question using NAMES, not numbers:

Top-level categories (use the exact name as "answer_type"):
  - "Selection-Based"
  - "Recall and Short Answer"
  - "Mathematical and Computational"
  - "Code and Programming"
  - "Diagrammatic and Visual"
  - "Table and Structured Data"
  - "Algorithm Execution and Tracing"
  - "Analysis and Interpretation"
  - "Design and Open-Ended"
  - "Essay and Explanation"

Subtypes (use the exact subtype name as "answer_subtype"):
{taxonomy}

Return ONLY valid JSON (no markdown fences):
{{
  "exam_info": {{
    "course_code": "...",
    "course_name": "...",
    "date": "...",
    "total_marks": null,
    "outcomes": [
      {{"outcome_number": 1, "total_marks": null, "description": "..."}}
    ]
  }},
  "questions_on_page": [
    {{
      "question_number": "Q1",
      "sub_id": "a",
      "outcome_number": 1,
      "question_text": "exact question text as written",
      "marks": 2,
      "answer_type": "Diagrammatic and Visual",
      "answer_subtype": "Network Diagram (AOA/AON)",
      "correct_answer": "exact correct answer as written in the key",
      "mcq_options": {{"a": "...", "b": "...", "c": "...", "d": "..."}},
      "mcq_correct_option": "a",
      "has_drawing": false,
      "drawing_description": null
    }}
  ]
}}

RULES:
1. Fill "exam_info" ONLY on page 1. On later pages, set "exam_info": null.
2. For each question or sub-question visible on this page, emit ONE object in "questions_on_page".
   Sub-parts (a, b, c...) → same question_number, different sub_id.
   No sub-parts → sub_id = "a".

QUESTION NUMBER RULE (CRITICAL — read carefully):
   A question number is ONLY a code or label that appears BEFORE the question text.
   Valid forms include:
     - "Q1.", "Q1", "Question 1", "Question 3:"
     - Bare numbers: "1.", "1)", "(1)", "2.", "3."
     - Letters: "(a)", "(b)", "a)"
   Use it exactly as written (e.g. if the document shows "1.", return question_number = "Q1"
   — always add the "Q" prefix so bare numbers become proper IDs like "Q1", "Q2", "Q3").

   IMPORTANT: numbering often RESTARTS within each outcome. "1.", "2.", "3." under
   OUTCOME 2, then "1.", "2." again under OUTCOME 4 is normal. This is fine — the
   caller uses (outcome_number, question_number) together as the unique key, so the
   same question_number under different outcomes does not collide.

   Any code that appears AFTER the question text is NEVER a question number.
   It is always either:
     (a) the marks allocation — e.g. "/3 marks", "(6 Marks)", "3 marks", "out of 4"
         → put the number into the "marks" field.
     (b) the instructor's Study Outcome or internal category code — e.g. "[SO 1.1]",
         "[SO 2.2]", "2.3", "4.1" written at the end of the line
         → IGNORE it completely. Do NOT use it as question_number. Do NOT put it in
         answer_subtype. These codes refer to the instructor's grading rubric, not the
         answer type taxonomy.

   Only if NO label at all appears before the question text, generate a synthetic unique
   ID of the form "O{{outcome_number}}-Q{{n}}" where n is the sequential position of the
   question within that outcome (e.g. "O2-Q1", "O2-Q2").

3. "marks" = marks allocated to THIS sub-question as a NUMBER (integer or decimal, e.g. 2, 2.5, 0.5), or null if not shown.
4. "answer_type" = the top-level category NAME (e.g. "Diagrammatic and Visual").
5. "answer_subtype" = the specific subtype NAME (e.g. "Network Diagram (AOA/AON)").
6. If answer_type = "Diagrammatic and Visual", set has_drawing = true and write a detailed
   drawing_description (nodes, arrows, labels, durations, shapes, colors, annotations).
7. For answer_type = "Selection-Based": fill mcq_options with all choices and
   mcq_correct_option with the correct letter. For other types, set both to null.
8. Transcribe any non-Latin script (Arabic, etc.) in its original script. Preserve formulas exactly.
9. If the page has no questions, return "questions_on_page": [].
10. Return ONLY valid JSON. No text outside the JSON object. No trailing commas.
"""


def _extract_page(client: Anthropic, page_b64: str,
                  page_number: int, total_pages: int) -> dict:
    prompt = _PAGE_PROMPT_TEMPLATE.format(
        page_number=page_number,
        total_pages=total_pages,
        taxonomy=_TAXONOMY_PROMPT_TEXT,
    )
    print(f"[answer-key-runner] calling Claude for page {page_number}/{total_pages}")
    response = _call_claude_with_retry(
        client,
        model=_MODEL,
        max_tokens=4000,
        temperature=0.0,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {
                "type": "base64", "media_type": "image/png", "data": page_b64,
            }},
            {"type": "text", "text": prompt},
        ]}],
    )
    print(f"[answer-key-runner] Claude response received for page {page_number}, stop_reason={response.stop_reason}")
    text = response.content[0].text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            return json.loads(_repair_json(text))
        except json.JSONDecodeError:
            return {"page_number": page_number, "raw_response": text, "parse_error": True}


# ── Public entry point ──────────────────────────────────────────────────────────

def run_answer_key_extraction(
    file_path: str,
    api_key: str,
    output_dir: str,
) -> dict:
    print(f"[answer-key-runner] run_answer_key_extraction called file={file_path} output_dir={output_dir}")
    """
    Extract a structured answer key from a PDF or DOCX file.

    Parameters
    ----------
    file_path  : absolute path to the answer key PDF or DOCX
    api_key    : Anthropic API key
    output_dir : directory where output files (JSON, drawing images) are written

    Returns
    -------
    dict  —  the answer_key_structured schema (matches notebook output):
      {
        "source_file": "...",
        "exam_info":   { "course_code", "course_name", "date" },
        "outcomes": [
          {
            "outcome_number": 1,
            "description": "...",
            "questions": [
              {
                "question_number": "Q1",
                "question_text":   "...",
                "sub_questions": [
                  {
                    "sub_id":               "a",
                    "sub_question_text":    "...",
                    "answer_type":          "...",
                    "answer_subtype":       "...",
                    "correct_answer":       "...",
                    "mcq_options":          null | {...},
                    "mcq_correct_option":   null | "a",
                    "has_drawing":          false,
                    "drawing_description":  null,
                    "answer_key_image_path": null,
                    "source_page":          1
                  }
                ]
              }
            ]
          }
        ]
      }

    Marks policy
    ------------
    All marks fields present in the notebook JSON schema
    (total_marks, outcome total_marks, sub_question marks) are NOT included in
    the returned dict.  Marks remain manual in the UI.
    """
    client = _make_client(api_key)
    source = Path(file_path)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ak_drawings_dir = out_dir / "answer_key_drawings"
    ak_drawings_dir.mkdir(parents=True, exist_ok=True)

    pages = _answer_key_to_images(source, dpi=200)

    exam_info: Optional[dict] = None
    all_sub_questions: list[dict] = []

    for i, page_img in enumerate(pages):
        page_num = i + 1
        page_b64 = _image_to_base64(page_img)
        page_data = _extract_page(client, page_b64, page_num, len(pages))

        if page_data.get("parse_error"):
            print(f"[answer-key-page] page_number={page_num} status=parse_error")
            continue

        qs_on_page = page_data.get("questions_on_page", [])
        print(f"[answer-key-page] page_number={page_num} status=success questions_on_page={len(qs_on_page)}")

        if page_data.get("exam_info") and exam_info is None:
            exam_info = page_data["exam_info"]

        for sq in qs_on_page:
            sq["source_page"] = page_num

            if sq.get("answer_type") == "Diagrammatic and Visual" or sq.get("has_drawing"):
                qnum = sq.get("question_number", "Q?").replace(".", "_")
                sub = sq.get("sub_id", "a")
                img_name = f"answer_key_{qnum}{sub}_page{page_num}.png"
                img_path = ak_drawings_dir / img_name
                page_img.save(str(img_path), "PNG")
                sq["answer_key_image_path"] = str(img_path)
                sq["has_drawing"] = True
                print(f"[diagram-detected] question_id={qnum}{sub} image_path={img_path}")

            all_sub_questions.append(sq)

    # Re-number synthetic IDs globally per outcome (same logic as notebook)
    synthetic_counters: dict[int, int] = {}
    for sq in all_sub_questions:
        qnum = sq.get("question_number", "")
        onum = sq.get("outcome_number") or 1
        if re.match(r"^O\d+-Q\d+$", str(qnum)):
            synthetic_counters[onum] = synthetic_counters.get(onum, 0) + 1
            sq["question_number"] = f"O{onum}-Q{synthetic_counters[onum]}"

    # Regroup: outcomes → questions → sub_questions
    outcomes_map: dict[int, dict] = {}
    for sq in all_sub_questions:
        onum = sq.get("outcome_number") or 1
        qnum = sq.get("question_number", "Q?")
        if onum not in outcomes_map:
            outcomes_map[onum] = {"questions": {}}
        if qnum not in outcomes_map[onum]["questions"]:
            outcomes_map[onum]["questions"][qnum] = {
                "question_number": qnum,
                "question_text":   sq.get("question_text", ""),
                "sub_questions":   [],
            }
        outcomes_map[onum]["questions"][qnum]["sub_questions"].append({
            "sub_id":               sq.get("sub_id", "a"),
            "sub_question_text":    sq.get("question_text", ""),
            "answer_type":          sq.get("answer_type", ""),
            "answer_subtype":       sq.get("answer_subtype", ""),
            "correct_answer":       sq.get("correct_answer", ""),
            "mcq_options":          sq.get("mcq_options"),
            "mcq_correct_option":   sq.get("mcq_correct_option"),
            "has_drawing":          sq.get("has_drawing", False),
            "drawing_description":  sq.get("drawing_description"),
            "answer_key_image_path": sq.get("answer_key_image_path"),
            "source_page":          sq.get("source_page"),
            # NOTE: "marks" field from notebook output is intentionally excluded
        })

    outcomes_meta = (exam_info or {}).get("outcomes", []) if exam_info else []
    outcomes_list = []
    for onum in sorted(outcomes_map.keys()):
        meta = next((o for o in outcomes_meta if o.get("outcome_number") == onum), {})
        questions_list = list(outcomes_map[onum]["questions"].values())
        outcomes_list.append({
            "outcome_number": onum,
            # "total_marks" intentionally excluded
            "description":    meta.get("description"),
            "questions":      questions_list,
        })

    answer_key = {
        "source_file": source.name,
        "exam_info": {
            "course_code":  (exam_info or {}).get("course_code"),
            "course_name":  (exam_info or {}).get("course_name"),
            "date":         (exam_info or {}).get("date"),
            # "total_marks" intentionally excluded
        },
        "outcomes": outcomes_list,
    }

    total_extracted = sum(
        len(q.get("sub_questions", []))
        for outcome in outcomes_list
        for q in outcome.get("questions", [])
    )
    print(f"[answer-key-extraction-complete] questions_extracted={total_extracted}")

    # Write output JSON
    out_path = out_dir / "answer_key_structured.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(answer_key, f, indent=2, ensure_ascii=False)
    print(f"[answer-key-runner] output JSON written to {out_path}")

    return answer_key
