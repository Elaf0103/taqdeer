"""
Grading Engine
==============
Core orchestration layer for the Taqdeer auto-grading pipeline.

Responsibilities:
  - Load exam questions and student extractions from the DB
  - Route each question to the correct grading mode:
      • deterministic  — MCQ and True/False (no LLM needed)
      • individual     — one or more models grade independently in parallel
      • consensus      — 3 models grade in two rounds (initial → revision)
  - Store raw per-model results in model_grading_results
  - Compute and store final consolidated scores in grading_final_scores
  - Populate exam_results and student_question_scores (the tables the
    existing results UI already reads from)
  - Update GradingJob status and progress

This module is called exclusively from routers/grading.py as a background
task.  It does NOT modify extraction tables or the supervisor notebook.

Grading is batched per question: one LLM call covers all students for a
single question, improving consistency and reducing cost.
"""

from __future__ import annotations

import base64
import concurrent.futures as _cf
import difflib
import json
import mimetypes
import os
import re
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from decimal import Decimal
from typing import Optional

from services.decimal_utils import D


# ── Timeout / safety constants ────────────────────────────────────────────────
# How long to wait for a single consensus phase (both models combined).
# Must be > LLM_CALL_TIMEOUT_SECS to allow the SDK timeout to fire first.
_PHASE_TIMEOUT_SECS: int  = 180   # 3 min per consensus phase

# Absolute ceiling on the entire grading job.  If reached, job is marked failed.
_JOB_MAX_SECS: int        = 7200  # 2 hours

# Maximum number of student drawing images attached to any single LLM call.
# With many students all submitting diagram answers, capping this prevents
# API stalls caused by excessively large multimodal payloads.
_MAX_STUDENT_IMAGES: int  = 4

from sqlalchemy.orm import Session

from database import SessionLocal
from models import (
    Exam, ExamModelAnswer, ExamResult, ExamStatus, GradeStatus,
    GradingFinalScore, GradingJob, GradingJobStatus, GradingPhase,
    GradingReport, ModelGradingResult, Rubric, StudentAnswerItem,
    StudentExtraction, StudentQuestionScore,
)
from services.grading_models import GradingModelClient, all_clients, get_model_client, run_health_check
from services.grading_prompts import (
    build_consensus_phase1_prompt,
    build_consensus_phase2_prompt,
    build_deepseek_imperfect_prompt,
    build_individual_prompt,
)


# ── Answer types that require LLM grading ────────────────────────────────────

_LLM_GRADED_TYPES = {
    "recall_general",
    "recall_differentiation",
    "mathematical",
    "diagrammatic_aoa",
    "diagrammatic_general",
}

_DETERMINISTIC_TYPES = {
    "multiple_choice",
    "true_false",
}


# ── Model → debug info mapping (env var name + default base URL) ─────────────
_MODEL_DEBUG_INFO: dict[str, tuple[str, str]] = {
    # model_key: (env_var_name, default_base_url_or_empty)
    "claude":   ("ANTHROPIC_API_KEY",  "https://api.anthropic.com"),
    "qwen":     ("QWEN_API_KEY",       "https://openrouter.ai/api/v1"),
    "deepseek": ("DEEPSEEK_API_KEY",   "https://api.deepseek.com/v1"),
    "openai":   ("OPENAI_API_KEY",     "https://api.openai.com/v1"),
}


# ── DeepSeek isolated research mode ──────────────────────────────────────────
#
#  ISOLATION CONTRACT
#  ------------------
#  When True, DeepSeek receives deepseek_imperfect_grading_prompt instead of
#  the standard individual_grading_prompt_template.
#
#  Scope of effect
#  ---------------
#  • Affects ONLY the prompt sent to model_key == "deepseek" in individual mode.
#  • Does NOT touch Claude, Qwen, or OpenAI prompt construction in any way.
#  • Does NOT affect consensus mode — DeepSeek is already excluded from
#    all_clients(), so it is structurally unreachable from the consensus path.
#  • Does NOT change scoring normalization, guards, or result parsing.
#  • Does NOT mutate any shared state; prompt selection is local to each call.
#
#  When False
#  ----------
#  DeepSeek behaves exactly like any other individual-mode model and receives
#  the standard individual_grading_prompt_template.
#
#  Toggle this flag and restart the backend to switch modes.
#  This flag has zero effect on any model other than DeepSeek.
#
DEEPSEEK_RESEARCH_MODE: bool = True


# ── Recall / list question scoring guard ─────────────────────────────────────

_RECALL_LIST_TYPES = {"recall_general", "recall_differentiation"}

_WORD_TO_INT: dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

# Common English words excluded from unique-keyword matching
_KW_STOPWORDS: frozenset[str] = frozenset({
    "the", "and", "for", "are", "but", "not", "you", "all", "can",
    "her", "was", "one", "our", "had", "his", "has", "its", "may",
    "did", "get", "use", "let", "put", "see", "try", "ask", "end",
    "way", "new", "two", "how", "who", "oil", "sit", "now", "him",
    "say", "she", "too", "any", "day", "men", "own", "old", "come",
    "via", "per", "etc",
})


def _detect_required_items(unit: dict) -> Optional[int]:
    """
    Infer how many items the question requires.
    Returns None when the count cannot be determined reliably.

    Priority:
      1. Question text: "list 4 …", "name three …", etc.
      2. max_score as integer (valid when each item = 1 mark, 1–10 items).
    """
    q_text = (unit.get("question_text") or "").lower()
    _trigger = (
        r"(?:list|write|name|mention|identify|state|give|enumerate|recall|describe)"
    )

    # Digit form: "list 4", "name 3 …"
    m = re.search(_trigger + r"\s+(\d+)", q_text)
    if m:
        return int(m.group(1))

    # Word form: "write four", "name three …"
    m = re.search(_trigger + r"\s+(one|two|three|four|five|six|seven|eight|nine|ten)", q_text)
    if m:
        return _WORD_TO_INT.get(m.group(1))

    # Fall back to integer max_score (1–10 range only; avoids false positives)
    max_s = float(unit.get("max_score") or 0)
    if max_s > 0 and max_s == int(max_s) and 1 <= int(max_s) <= 10:
        return int(max_s)

    return None


def _detect_matched_items(result: dict, required: int) -> Optional[int]:
    """
    Infer how many items the model confirmed as correct.

    Priority:
      1. len(matched_points) when the list is non-empty.
      2. Regex on the reason text for "N correct", "identified N", etc.

    Returns None when uncertain (guard is skipped for this student).
    """
    mp = result.get("matched_points")
    if isinstance(mp, list) and len(mp) > 0:
        return min(len(mp), required)

    reason = (result.get("reason") or "").lower()

    # Digit patterns in reason text
    for pat in (
        r"(\d+)\s+(?:correct|matched|identified|found|valid|accepted)",
        r"(?:identified|found|matched|counted|accepted)\s+(\d+)",
        r"(\d+)\s+out\s+of\s+\d+\s+(?:are\s+)?correct",
    ):
        m = re.search(pat, reason)
        if m:
            n = int(m.group(1))
            if 0 <= n <= required:
                return n

    # Word-number patterns in reason text
    for word, val in _WORD_TO_INT.items():
        if re.search(
            rf"\b{word}\b.{{0,30}}(?:correct|matched|identified|found|valid|accepted)",
            reason,
        ):
            if 0 <= val <= required:
                return val

    return None


def _apply_recall_list_guard(results: list[dict], unit: dict) -> list[dict]:
    """
    Post-parse, pre-store guard for recall / list questions.

    If the model identified N correct concepts in its reasoning but assigned a
    score lower than (N / required_items) × max_score, raises the score to that
    minimum.  The caller still applies _round_quarter afterwards.

    Guard is skipped when:
    - answer_type is not a recall/list type AND question text has no list keyword
    - scoring_method contains "exact", "keyword_only", or "strict"
    - required_items or matched_items cannot be determined

    Logs [RECALL-LIST-GUARD] for every raised score.
    """
    answer_type    = unit.get("answer_type", "")
    q_text         = (unit.get("question_text") or "").lower()
    scoring_method = (unit.get("scoring_method") or "").lower()
    qid            = unit.get("question_id", "?")
    max_score      = float(unit.get("max_score") or 0)

    # Is this a recall / list question?
    is_recall = answer_type in _RECALL_LIST_TYPES
    if not is_recall:
        list_kws = ("list", "name", "mention", "identify", "enumerate",
                    "recall", "state", "give", "write")
        is_recall = any(kw in q_text for kw in list_kws)
    if not is_recall:
        return results

    # Skip exact / strict scoring modes
    if any(s in scoring_method for s in ("exact", "keyword_only", "strict")):
        return results

    if max_score <= 0:
        return results

    required = _detect_required_items(unit)
    if required is None:
        return results

    guarded: list[dict] = []
    for r in results:
        r = dict(r)  # shallow copy — never mutate caller's dict
        raw_score = float(r.get("assigned_score", 0))
        sid       = r.get("student_id", "?")

        matched = _detect_matched_items(r, required)
        if matched is None:
            guarded.append(r)
            continue

        minimum = (matched / required) * max_score

        if raw_score < minimum - 0.001:  # epsilon guards against float noise
            guarded_score = min(minimum, max_score)
            guard_note = (
                f"Score adjusted by recall-list consistency guard: "
                f"model identified {matched} correct concept(s) out of "
                f"{required} required."
            )
            old_reason = r.get("reason", "")
            r["assigned_score"] = guarded_score
            r["reason"] = (
                f"{old_reason}\n{guard_note}".strip() if old_reason else guard_note
            )
            print(
                f"[RECALL-LIST-GUARD] "
                f"question_id={qid} "
                f"student_id={sid} "
                f"required_items={required} "
                f"matched_items={matched} "
                f"raw_score={raw_score} "
                f"guarded_score={guarded_score}"
            )

        guarded.append(r)

    return guarded


def _extract_answer_key_items(answer_key_text: str) -> list[str]:
    """
    Parse an answer key string into individual item strings.

    Handles:
      - Numbered lists:  "1. item"  or  "1) item"
      - Bullet lists:    "- item"   or  "* item"  or  "• item"
      - Plain newline-separated lines

    Returns an empty list when no parseable items are found (guard is skipped).
    """
    if not answer_key_text or not answer_key_text.strip():
        return []
    text = answer_key_text.strip()

    # Numbered
    numbered = re.findall(r'^\s*\d+[\.\)]\s*(.+)$', text, re.MULTILINE)
    if len(numbered) >= 2:
        return [i.strip() for i in numbered if i.strip()]

    # Bullet
    bulleted = re.findall(r'^\s*[-*•]\s*(.+)$', text, re.MULTILINE)
    if len(bulleted) >= 2:
        return [i.strip() for i in bulleted if i.strip()]

    # Newline-separated (require at least 2 non-trivial lines)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and len(ln.strip()) > 2]
    if len(lines) >= 2:
        return lines

    return []


def _extract_significant_keywords(item_text: str) -> set[str]:
    """
    Extract candidate keywords: lower-cased alpha words, length ≥ 3,
    not in _KW_STOPWORDS.
    """
    words = re.findall(r'\b[a-z]{3,}\b', item_text.lower())
    return {w for w in words if w not in _KW_STOPWORDS}


def _build_unique_keyword_map(items: list[str]) -> dict[str, str]:
    """
    Return {keyword: item_text} for keywords that appear in exactly one item.

    Keywords shared across multiple items are excluded — they are not
    distinctive enough to identify a single concept.
    """
    kw_to_items: dict[str, list[str]] = {}
    for item in items:
        for kw in _extract_significant_keywords(item):
            kw_to_items.setdefault(kw, []).append(item)
    return {kw: lst[0] for kw, lst in kw_to_items.items() if len(lst) == 1}


def _fuzzy_keyword_hit(kw: str, student_words: set[str]) -> Optional[str]:
    """
    Return the first student word that matches `kw` either exactly or via
    OCR-tolerant fuzzy comparison.

    Exact match is tried first (O(1)).  Fuzzy match uses
    difflib.SequenceMatcher with a similarity threshold that scales with
    keyword length:
      - length 3:   exact only (too short for reliable fuzzy matching)
      - length 4-5: similarity ≥ 0.75
      - length 6+:  similarity ≥ 0.70

    The fuzzy layer catches common OCR distortions:
      "sekils" → "skills",  "flikbl" → "flexible",  "proactv" → "proactive"

    Returns the matched keyword string (for logging) or None if no match.
    """
    # 1. Exact
    if kw in student_words:
        return kw

    # 2. Fuzzy — only for keywords ≥ 4 chars (shorter ones risk false positives)
    if len(kw) < 4:
        return None

    threshold = 0.75 if len(kw) <= 5 else 0.70
    for sw in student_words:
        # Rough length guard: student word must be at least 60 % of keyword length
        if len(sw) < len(kw) * 0.60:
            continue
        ratio = difflib.SequenceMatcher(None, sw, kw).ratio()
        if ratio >= threshold:
            return kw   # return the canonical keyword, not the distorted student word

    return None


def _apply_recall_keyword_match_guard(
    results: list[dict],
    unit: dict,
    student_batch: list[dict],
) -> list[dict]:
    """
    GLOBAL keyword-based recall matching guard.

    Applies to ALL students, ALL recall/list questions, ALL grading modes.
    Called in individual mode before _apply_recall_list_guard, and in
    consensus mode before _apply_recall_list_guard_consensus.

    Algorithm
    ---------
    1. Parse the answer key into individual items.
    2. Build a unique-keyword map: keyword → item (keyword appears in exactly
       one AK item — if it appears in multiple items it is not distinctive).
    3. For each student, check whether any un-accepted AK item has a unique
       keyword present in the student's raw answer text (exact or fuzzy match).
    4. If found: add the item to matched_points, remove from missing_points.
    5. Log [GLOBAL-KEYWORD-MATCH] — EVERY student, EVERY keyword match.

    Consistency guarantee
    ---------------------
    The unique-keyword map is built from the answer key ONLY, not from any
    per-student data.  Every student is processed with the identical map.
    A keyword match for student A will fire identically for student B if
    their raw answer contains the same keyword.

    OCR tolerance
    -------------
    Uses _fuzzy_keyword_hit which accepts OCR-distorted words:
      "flikbl" → "flexible",  "sekils" → "skills",  "proactv" → "proactive"

    Guard is skipped when:
    - answer_type is not recall/list AND no list keyword in question text
    - scoring_method contains "exact", "keyword_only", or "strict"
    - answer_key_text cannot be parsed into ≥ 2 items
    - no unique keywords can be extracted from the answer key

    Does NOT mutate caller's dicts (shallow-copies every result).
    """
    answer_type     = unit.get("answer_type", "")
    q_text          = (unit.get("question_text") or "").lower()
    scoring_method  = (unit.get("scoring_method") or "").lower()
    answer_key_text = unit.get("answer_key_text") or ""
    qid             = unit.get("question_id", "?")

    # Is this a recall / list question?
    is_recall = answer_type in _RECALL_LIST_TYPES
    if not is_recall:
        list_kws = ("list", "name", "mention", "identify", "enumerate",
                    "recall", "state", "give", "write")
        is_recall = any(kw in q_text for kw in list_kws)
    if not is_recall:
        return results

    # Skip exact / strict scoring modes
    if any(sm in scoring_method for sm in ("exact", "keyword_only", "strict")):
        return results

    # Parse answer key into items; need ≥ 2 for a meaningful uniqueness map
    ak_items = _extract_answer_key_items(answer_key_text)
    if len(ak_items) < 2:
        return results

    # Build unique keyword → item_text map (same for every student — consistency)
    unique_kw_map = _build_unique_keyword_map(ak_items)
    if not unique_kw_map:
        return results

    # {student_id → raw answer text (lower-cased)} from student_batch
    answer_by_sid: dict[str, str] = {
        s["student_id"]: (s.get("answer") or "").lower()
        for s in student_batch
    }

    guarded: list[dict] = []
    for r in results:
        r = dict(r)  # shallow copy — never mutate caller's dict
        sid          = r.get("student_id", "?")
        student_text = answer_by_sid.get(sid, "")

        if not student_text:
            guarded.append(r)
            continue

        matched_pts: list = list(r.get("matched_points") or [])
        missing_pts: list = list(r.get("missing_points") or [])

        # Word-token set from raw answer for fast lookup
        student_words = set(re.findall(r'\b[a-z]+\b', student_text))
        # Accepted text (lower-cased) for de-duplication
        accepted_text = " ".join(str(m) for m in matched_pts).lower()

        for ak_item in ak_items:
            item_kws = _extract_significant_keywords(ak_item)
            # Unique keywords that identify THIS specific item
            unique_kws = {kw for kw in item_kws
                          if kw in unique_kw_map and unique_kw_map[kw] == ak_item}
            if not unique_kws:
                continue

            # Skip if LLM already accepted this item
            if any(kw in accepted_text for kw in unique_kws):
                continue

            # Check student words — longer keywords tried first (more specific)
            hit_kw: Optional[str] = None
            for kw in sorted(unique_kws, key=len, reverse=True):
                hit_kw = _fuzzy_keyword_hit(kw, student_words)
                if hit_kw:
                    break

            if hit_kw:
                matched_pts.append(ak_item)
                missing_pts = [
                    m for m in missing_pts
                    if hit_kw not in str(m).lower()
                    and ak_item.lower() not in str(m).lower()
                ]
                accepted_text += f" {ak_item.lower()}"
                print(
                    f"[GLOBAL-KEYWORD-MATCH] "
                    f"student_id={sid} "
                    f"question_id={qid} "
                    f"raw_text=\"{student_text[:100]}\" "
                    f"keyword_detected={hit_kw} "
                    f"mapped_to=\"{ak_item}\" "
                    f"accepted=true"
                )

        r["matched_points"] = matched_pts
        r["missing_points"] = missing_pts
        guarded.append(r)

    return guarded


def _apply_recall_list_guard_consensus(
    outcome: dict,
    unit: dict,
    student_batch: list[dict],
) -> dict[str, dict]:
    """
    Post-consensus guard for recall / list questions.

    Applies the same score-reasoning consistency check as
    _apply_recall_list_guard, but operates on the consensus outcome dict
    produced by _grade_consensus (which already contains quarter-rounded
    final_scores).

    Returns {student_id: {"score": float, "reason": str}} for every student.
    Scores that need no correction are returned unchanged.

    Phase 1 initial_results supply matched_points (structured list).
    Phase 2 revised_results supply the most up-to-date reason text.

    The caller is responsible for applying _round_quarter after this guard.
    Logs [RECALL-LIST-GUARD-CONSENSUS] for every raised score.
    """
    answer_type    = unit.get("answer_type", "")
    q_text         = (unit.get("question_text") or "").lower()
    scoring_method = (unit.get("scoring_method") or "").lower()
    qid            = unit.get("question_id", "?")
    max_score      = float(unit.get("max_score") or 0)

    # Is this a recall / list question?
    is_recall = answer_type in _RECALL_LIST_TYPES
    if not is_recall:
        list_kws = ("list", "name", "mention", "identify", "enumerate",
                    "recall", "state", "give", "write")
        is_recall = any(kw in q_text for kw in list_kws)

    # Required-items count (used by all students; compute once)
    required = _detect_required_items(unit) if is_recall else None

    # Phase 1 Examiner-1 results keyed by student_id
    initial_by_sid: dict[str, dict] = {}
    for r in outcome.get("initial_results", []):
        sid = r.get("student_id", "")
        if sid and r.get("examiner_label") == "Examiner 1":
            initial_by_sid[sid] = r

    # Phase 2 revised results keyed by student_id
    revised_by_sid: dict[str, dict] = {}
    for r in outcome.get("revised_results", []):
        sid = r.get("student_id", "")
        if sid:
            revised_by_sid.setdefault(sid, r)

    guarded: dict[str, dict] = {}
    for s in student_batch:
        sid    = s["student_id"]
        fscore = float(outcome["final_scores"].get(sid, 0.0))

        # Best available reason: prefer Phase-2 text, fall back to Phase-1
        phase2 = revised_by_sid.get(sid, {})
        phase1 = initial_by_sid.get(sid, {})
        rep_reason = (
            phase2.get("reason") or phase2.get("revision_reason")
            or phase1.get("reason", "")
        )

        # Guard skip conditions
        skip = (
            not is_recall
            or any(sm in scoring_method for sm in ("exact", "keyword_only", "strict"))
            or max_score <= 0
            or required is None
        )
        if skip:
            guarded[sid] = {"score": fscore, "reason": rep_reason}
            continue

        # Synthetic result dict for _detect_matched_items:
        # combine the latest reason text with Phase-1 matched_points
        synthetic = {
            "assigned_score": fscore,
            "reason":         rep_reason,
            "matched_points": phase1.get("matched_points", []),
        }
        matched = _detect_matched_items(synthetic, required)  # type: ignore[arg-type]
        if matched is None:
            guarded[sid] = {"score": fscore, "reason": rep_reason}
            continue

        minimum = (matched / required) * max_score  # type: ignore[operator]
        if fscore < minimum - 0.001:
            guarded_score = min(minimum, max_score)
            guard_note = (
                f"Final consensus score adjusted by recall-list consistency guard: "
                f"model identified {matched} correct concept(s) out of {required} required."
            )
            new_reason = (
                f"{rep_reason}\n{guard_note}".strip() if rep_reason else guard_note
            )
            print(
                f"[RECALL-LIST-GUARD-CONSENSUS] "
                f"question_id={qid} "
                f"student_id={sid} "
                f"required_items={required} "
                f"matched_items={matched} "
                f"raw_final_score={fscore} "
                f"guarded_final_score={guarded_score}"
            )
            guarded[sid] = {"score": guarded_score, "reason": new_reason}
        else:
            guarded[sid] = {"score": fscore, "reason": rep_reason}

    return guarded


# ── LLM call wrapper with timing logs ────────────────────────────────────────

def _timed_complete(
    client,
    prompt: str,
    images,
    question_id: str,
    phase: str,
) -> str:
    """
    Call client.complete() and emit [LLM-CALL-START/END/TIMEOUT/ERROR] log lines.

    On error, logs: status_code, provider error message, model name,
    API endpoint, and env var name (key masked to last 4 chars).

    Raises the original exception on failure (caller handles fallback).
    """
    print(
        f"[LLM-CALL-START] question_id={question_id} "
        f"model={client.name} phase={phase} "
        f"images={len(images) if images else 0}"
    )
    t0 = time.time()
    try:
        result = client.complete(prompt, images=images)
        elapsed = time.time() - t0
        print(
            f"[LLM-CALL-END] question_id={question_id} "
            f"model={client.name} phase={phase} seconds={elapsed:.1f}"
        )
        return result
    except Exception as exc:
        elapsed  = time.time() - t0
        cls      = type(exc).__name__
        is_timeout = (
            "Timeout" in cls
            or "timeout" in str(exc).lower()
            or "timed out" in str(exc).lower()
        )

        # ── Extract structured error detail ───────────────────────────────────
        status_code  = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        body         = getattr(exc, "body", None)
        provider_msg = ""
        if isinstance(body, dict):
            err_obj = body.get("error", body)
            if isinstance(err_obj, dict):
                provider_msg = (
                    err_obj.get("message", "")
                    or err_obj.get("code", "")
                    or err_obj.get("type", "")
                )
            elif isinstance(err_obj, str):
                provider_msg = err_obj
        if not provider_msg:
            provider_msg = str(exc)[:400]

        # ── Env var + endpoint lookup ─────────────────────────────────────────
        env_var, default_base = _MODEL_DEBUG_INFO.get(client.name, ("UNKNOWN_API_KEY", ""))
        # For openai the base_url may be overridden via OPENAI_BASE_URL
        if client.name == "openai":
            actual_base = os.environ.get("OPENAI_BASE_URL", "").strip() or default_base
        else:
            actual_base = default_base

        # Masked key — last 4 chars only; never the raw value
        raw_key    = os.environ.get(env_var, "")
        key_suffix = f"…{raw_key[-4:]}" if len(raw_key) >= 4 else "(not set)"

        if is_timeout:
            print(
                f"[LLM-CALL-TIMEOUT] question_id={question_id} "
                f"model={client.name} phase={phase} seconds={elapsed:.1f}"
            )
        else:
            print(
                f"[LLM-CALL-ERROR] question_id={question_id} "
                f"model={client.name} phase={phase} "
                f"seconds={elapsed:.1f} "
                f"error_class={cls} "
                f"status_code={status_code} "
                f"provider_error={provider_msg[:300]!r} "
                f"endpoint={actual_base} "
                f"key_source={env_var}(ending {key_suffix})"
            )
        raise


# ── Score normalisation ───────────────────────────────────────────────────────

def normalize_score(score, max_score) -> float:
    """
    Normalise a raw student score to the nearest allowed quarter increment,
    then apply the no-.75 rule, and clamp to [0, max_score].

    Allowed output fractional parts: .00, .25, .50  (never .75)

    Algorithm
    ---------
    1. Round the raw float to the nearest 0.25 increment.
    2. If the fractional part is .75, round UP to the next whole number.
    3. Clamp to [0, max_score].

    Examples
    --------
    0.17  → 0.25
    0.45  → 0.50
    1.33  → 1.25
    1.62  → 1.50
    1.63  → 2.00  (nearest 0.25 = 1.75 → .75 rule → 2.00)
    1.74  → 2.00  (nearest 0.25 = 1.75 → .75 rule → 2.00)
    1.75  → 2.00  (.75 rule)
    1.76  → 2.00  (nearest 0.25 = 1.75 → .75 rule → 2.00)
    0.75  → 1.00  (.75 rule)
    2.75  → 3.00  (.75 rule)
    1.88  → 2.00
    """
    score_f     = float(score)
    max_score_f = float(max_score)

    # Step 1: round to nearest 0.25
    rounded = round(score_f * 4) / 4

    # Step 2: no-.75 rule — if fractional part is .75, round up to next integer.
    # Use integer arithmetic on hundredths to avoid floating-point noise.
    hundredths = round(rounded * 100) % 100   # e.g. 1.75 → 75; 1.50 → 50
    if hundredths == 75:
        rounded = float(int(rounded) + 1)

    # Step 3: clamp
    result = float(max(0.0, min(rounded, max_score_f)))

    print(f"[SCORE-NORMALIZE] raw={score_f} normalized={result}")
    return result


# Keep the old name as an alias so any external callers still work
_round_quarter = normalize_score


# ══════════════════════════════════════════════════════════════════════════════
#  Pre-flight LLM health check
# ══════════════════════════════════════════════════════════════════════════════

def check_llm_health_for_exam(
    selected_models: list[str],
    consensus_enabled: bool,
    q_rows: list,
) -> tuple[bool, str]:
    """
    Verify that the selected LLM(s) can accept requests before a grading job
    is queued.  Sends a minimal "Reply with OK" test prompt to each model.

    Returns
    -------
    (True, "")
        All required models responded successfully — safe to start grading.
    (False, error_message)
        At least one model is unreachable or unauthenticated.  The error
        message contains status code, provider error, endpoint, and env var
        name (key masked).  It NEVER contains the raw API key value.

    MCQ-only exemption
    ------------------
    If the exam contains only deterministic (MCQ / True-False) questions,
    LLM models are not used and the check is skipped entirely.

    Consensus note
    --------------
    When consensus_enabled=True, only Claude + OpenAI are checked regardless
    of the selected_models list (consensus always uses these two).
    """
    # ── Determine whether any questions require LLM grading ──────────────────
    all_units: list[dict] = []
    for row in q_rows:
        all_units.extend(_expand_question_units(row))

    llm_units = [u for u in all_units if u["answer_type"] not in _DETERMINISTIC_TYPES]
    if not llm_units:
        print("[HEALTH-CHECK] All questions are deterministic — skipping LLM health check")
        return True, ""

    # ── Determine which models will actually be called ────────────────────────
    if consensus_enabled:
        models_to_check = ["claude", "openai"]
    else:
        models_to_check = [m.lower().strip() for m in selected_models]

    print(
        f"[HEALTH-CHECK] Checking {len(models_to_check)} model(s): {models_to_check} "
        f"({len(llm_units)} LLM question unit(s) detected)"
    )

    # ── Run health check for each model ──────────────────────────────────────
    for model_name in models_to_check:
        ok, err = run_health_check(model_name)
        if not ok:
            return False, err

    return True, ""


# ══════════════════════════════════════════════════════════════════════════════
#  Data loading helpers
# ══════════════════════════════════════════════════════════════════════════════

def _load_questions(db: Session, exam_id: int) -> list[ExamModelAnswer]:
    return (
        db.query(ExamModelAnswer)
        .filter(ExamModelAnswer.exam_id == exam_id)
        .order_by(ExamModelAnswer.question_number)
        .all()
    )


def _load_extractions(db: Session, exam_id: int) -> list[dict]:
    """
    Return a list of student dicts, each containing:
      extraction_id, student_id, student_name, answers: {question_id: StudentAnswerItem}

    Deduplication: if the same student_id appears more than once (which the DB
    UNIQUE constraint should now prevent, but may still exist from old data), the
    newest extraction (highest id / latest extracted_at) wins and older duplicates
    are silently skipped with a warning log.
    """
    extractions = (
        db.query(StudentExtraction)
        .filter(StudentExtraction.exam_id == exam_id)
        .order_by(StudentExtraction.extracted_at.desc())   # newest first → wins dedup
        .all()
    )

    seen_ids: set[str] = set()
    deduped: list[StudentExtraction] = []
    for ext in extractions:
        sid = ext.student_id_num or f"STU-{ext.id:03d}"
        if sid in seen_ids:
            print(
                f"[grading-engine] WARN: skipping duplicate extraction_id={ext.id} "
                f"for student_id={sid!r} in exam_id={exam_id} (keeping newer record)"
            )
            continue
        seen_ids.add(sid)
        deduped.append(ext)

    result = []
    for ext in deduped:
        items = (
            db.query(StudentAnswerItem)
            .filter(StudentAnswerItem.extraction_id == ext.id)
            .all()
        )
        student_id = ext.student_id_num or f"STU-{ext.id:03d}"
        result.append({
            "extraction_id": ext.id,
            "student_id":    student_id,
            "student_name":  ext.student_name or "Unknown",
            "answers":       {item.question_id.upper(): item for item in items},
        })
    return result


def _extract_rubric_text(file_path: str) -> Optional[str]:
    """
    Extract plain text from a rubric PDF using PyMuPDF (fitz).
    Falls back gracefully if the library is unavailable or the file is unreadable.
    Returns the extracted text, or None on failure.
    """
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(file_path)
        pages = [doc[i].get_text() for i in range(len(doc))]
        doc.close()
        text = "\n\n".join(p for p in pages if p.strip())
        return text.strip() or None
    except ImportError:
        print("[RUBRIC] PyMuPDF not available — cannot extract rubric text")
    except Exception as exc:
        print(f"[RUBRIC] PDF extraction error for {file_path!r}: {exc}")
    return None


def _load_rubric_text(db: Session, exam_id: int) -> Optional[str]:
    """
    Return rubric text extracted from the rubric PDF, if available.
    Falls back to a placeholder string if extraction fails so the prompt
    still knows a rubric file exists.
    """
    rubric = db.query(Rubric).filter(Rubric.exam_id == exam_id).first()
    if not rubric or not rubric.file_path:
        return None
    print(f"[RUBRIC] Rubric file detected: {rubric.file_name}")
    extracted = _extract_rubric_text(rubric.file_path)
    if extracted:
        print(f"[RUBRIC] Rubric text extracted successfully ({len(extracted)} chars)")
        return extracted
    print(f"[RUBRIC] Rubric extraction failed — fallback to answer key")
    return f"[Rubric file available: {rubric.file_name} — text extraction pending]"


# ══════════════════════════════════════════════════════════════════════════════
#  Question unit expansion
# ══════════════════════════════════════════════════════════════════════════════

def _expand_question_units(row: ExamModelAnswer) -> list[dict]:
    """
    Convert one ExamModelAnswer row into one or more gradable units.

    A "unit" maps to exactly one LLM grading call (all students, one question).
    Grouped questions ({"__sub__": [...]}) produce one unit per sub-question.
    Flat questions produce a single unit.

    Returned dict keys:
      question_id      — "Q{N}" or "Q{N}a", "Q{N}b", …
      question_text    — the question text for this unit
      answer_key_text  — the correct answer string
      answer_type      — UI answer type code
      max_score        — float (from row.score; 0 if not yet set)
      scoring_method   — str or None
      additional_checks — str or None
      custom_instructions — str or None
    """
    qnum = f"Q{row.question_number}"
    ma   = row.model_answer or ""

    # Parse answer_key_images column (JSON array of file paths, or None/empty)
    _ak_col_paths: list[str] = []
    if row.answer_key_images:
        try:
            _ak_col_paths = json.loads(row.answer_key_images) or []
        except (json.JSONDecodeError, TypeError):
            _ak_col_paths = []

    base = {
        "max_score":                float(row.score or 0),
        "answer_type":              row.answer_type or "recall_general",
        "scoring_method":           row.scoring_method,
        "additional_checks":        row.additional_checks,
        "custom_instructions":      row.custom_instructions,
        "answer_key_images_column": _ak_col_paths,   # paths from DB column
    }

    if ma.startswith('{"__sub__":'):
        try:
            parsed   = json.loads(ma)
            sub_list = parsed.get("__sub__", [])
        except (json.JSONDecodeError, ValueError):
            sub_list = []

        if sub_list:
            # Default per-sub max: divide parent score equally (fallback when no override).
            per_sub_max = round(float(row.score or 0) / len(sub_list), 4)
            units = []
            for entry in sub_list:
                label = entry.get("label", "a")

                # Use instructor-supplied score_override when present and valid (> 0).
                # Fall back to the equal-division default otherwise.
                raw_override = entry.get("score_override")
                try:
                    override_val = float(raw_override) if raw_override is not None else None
                except (TypeError, ValueError):
                    override_val = None
                max_score = (override_val if override_val is not None and override_val > 0
                             else per_sub_max)

                units.append({
                    **base,
                    "max_score":       max_score,
                    "question_id":    f"{qnum}{label}".upper(),
                    "question_text":  entry.get("question_text", row.question_text),
                    "answer_key_text": entry.get("answer_key", ""),
                })
            return units

    # Flat question
    return [{
        **base,
        "question_id":    qnum.upper(),
        "question_text":  row.question_text,
        "answer_key_text": ma,
    }]


# ══════════════════════════════════════════════════════════════════════════════
#  Student answer collection per unit
# ══════════════════════════════════════════════════════════════════════════════

def _get_student_answers_for_unit(
    extractions: list[dict],
    unit: dict,
) -> list[dict]:
    """
    Collect each student's answer for the given unit (question_id).

    Returns a list of dicts:
      student_id, student_name, answer, selected_option,
      drawing_description, has_drawing, images (list of base64 dicts)
    """
    qid = unit["question_id"].upper()
    batch = []
    for student in extractions:
        item: Optional[StudentAnswerItem] = student["answers"].get(qid)
        if item is None:
            answer = ""
            selected_option = None
            has_drawing = False
            drawing_description = None
            images: list[dict] = []
        elif unit["answer_type"] in _DETERMINISTIC_TYPES:
            answer = item.student_answer or ""
            selected_option = item.student_selected_option
            has_drawing = False
            drawing_description = None
            images = []
        else:
            answer = item.student_answer or ""
            selected_option = None
            has_drawing = bool(item.has_drawing)
            drawing_description = item.drawing_description or None

            # Load drawing image for multimodal grading
            images = []
            if has_drawing and item.drawing_file_path:
                img = _load_image_as_base64(item.drawing_file_path)
                if img:
                    images.append(img)

        batch.append({
            "extraction_id":      student["extraction_id"],
            "student_id":         student["student_id"],
            "student_name":       student["student_name"],
            "answer":             answer,
            "selected_option":    selected_option,
            "has_drawing":        has_drawing,
            "drawing_description": drawing_description,
            "images":             images,
            "item":               item,   # raw ORM object for deterministic grading
        })

    matched_count = sum(1 for s in batch if s.get("item") is not None)
    print(
        f"[MATCH] question_id={unit['question_id']} "
        f"matched_answers={matched_count} students={len(batch)}"
    )
    if matched_count == 0 and len(batch) > 0:
        print(
            f"[MATCH-WARN] question_id={unit['question_id']} "
            f"no student answers matched — check question_id casing or extraction completeness"
        )

    drawing_count = sum(1 for s in batch if s.get("has_drawing"))
    if drawing_count:
        print(
            f"[GRADING] question_id={unit['question_id']} "
            f"student_diagrams_detected={drawing_count}/{len(batch)}"
        )

    return batch


# ══════════════════════════════════════════════════════════════════════════════
#  Deterministic grading (MCQ / True-False)
# ══════════════════════════════════════════════════════════════════════════════

def _grade_deterministic(unit: dict, student_batch: list[dict]) -> list[dict]:
    """
    Grade MCQ and True/False answers by exact string comparison.

    Correct → full marks.  Incorrect or blank → 0.
    """
    results = []
    answer_type = unit["answer_type"]

    # Parse correct answer from answer_key_text
    correct_option = None
    answer_key_raw = unit.get("answer_key_text", "")

    if answer_type == "multiple_choice":
        # answer_key_text may be JSON: {"type":"multiple_choice","options":[...]}
        # or a plain letter like "a" / "b"
        try:
            parsed = json.loads(answer_key_raw)
            if isinstance(parsed, dict):
                opts = parsed.get("options", [])
                # Options are serialised sorted by key ("a","b","c",...) so
                # position 0 → "a", 1 → "b", etc.  Prefer the explicit "key"
                # field written by the updated _serialize_model_answer; fall
                # back to position-based letter for older serialisations.
                for i, opt in enumerate(opts):
                    if opt.get("correct"):
                        letter = opt.get("key", "").strip().lower()
                        if not letter and i < 26:
                            letter = chr(ord("a") + i)
                        correct_option = letter
                        break
        except (json.JSONDecodeError, TypeError):
            correct_option = answer_key_raw.strip().lower()
        print(
            f"[MCQ-debug] question_id={unit['question_id']} "
            f"answer_key_raw={answer_key_raw!r} "
            f"correct_option={correct_option!r}"
        )

    elif answer_type == "true_false":
        try:
            parsed = json.loads(answer_key_raw)
            if isinstance(parsed, dict):
                tf = parsed.get("correct")
                if tf is True:
                    correct_option = "true"
                elif tf is False:
                    correct_option = "false"
        except (json.JSONDecodeError, TypeError):
            correct_option = answer_key_raw.strip().lower()

    for s in student_batch:
        item: Optional[StudentAnswerItem] = s.get("item")
        max_score = unit["max_score"]

        if item is None:
            # No answer recorded — use explicit float so the result dict is homogeneous.
            results.append({
                "extraction_id":  s["extraction_id"],
                "student_id":     s["student_id"],
                "assigned_score": 0.0,
                "max_score":      float(max_score),
                "basis_used":     "answer_key",
                "reason":         "No answer recorded for this student.",
                "matched_points": [],
                "missing_points": ["No answer provided"],
            })
            continue

        # For MCQ, compare student_selected_option against the correct option
        # For True/False, compare student_selected_option or student_answer
        if answer_type == "multiple_choice":
            student_sel = (item.student_selected_option or "").strip().lower()
            key_correct = (item.mcq_key_correct_option or correct_option or "").strip().lower()
            is_correct  = bool(student_sel and key_correct and student_sel == key_correct)
            print(
                f"[MCQ-debug] student={s['student_id']} "
                f"student_selected={student_sel!r} "
                f"mcq_key_correct_option={item.mcq_key_correct_option!r} "
                f"correct_option={correct_option!r} "
                f"key_correct={key_correct!r} "
                f"is_correct={is_correct} "
                f"score={unit['max_score'] if is_correct else 0.0}/{unit['max_score']}"
            )
        else:  # true_false
            student_sel = (
                item.student_selected_option or item.student_answer or ""
            ).strip().lower()
            # Normalise
            if student_sel in ("true", "t", "yes", "صح", "صواب", "1"):
                student_norm = "true"
            elif student_sel in ("false", "f", "no", "خطأ", "خطا", "0"):
                student_norm = "false"
            else:
                student_norm = student_sel
            key_correct = (item.mcq_key_correct_option or correct_option or "").strip().lower()
            is_correct  = bool(student_norm and key_correct and student_norm == key_correct)

        # Always store as float so downstream sum(per_model_scores) stays homogeneous.
        assigned = float(max_score) if is_correct else 0.0
        results.append({
            "extraction_id":  s["extraction_id"],
            "student_id":     s["student_id"],
            "assigned_score": assigned,           # float
            "max_score":      float(max_score),   # float
            "basis_used":     "answer_key",
            "reason":         "Correct." if is_correct else f"Incorrect. Correct answer: {key_correct or '?'}",
            "matched_points": ["Correct answer selected"] if is_correct else [],
            "missing_points": [] if is_correct else ["Incorrect answer selected"],
        })

    return results


# ══════════════════════════════════════════════════════════════════════════════
#  LLM response parsing
# ══════════════════════════════════════════════════════════════════════════════

def _parse_grading_response(
    raw: str,
    student_batch: list[dict],
    max_score: float,
    phase: str,
) -> list[dict]:
    """
    Parse a model's JSON response into a normalised list of result dicts.

    Handles both output shapes produced by the canonical prompt templates:

    Individual / consensus Phase 1
      Template output: {"question_id": "…", "results": [{student_id, …}, …]}
      Legacy fallback:  [{student_id, …}, …]  (plain array)

    Consensus Phase 2 (discussion round)
      Template output: [{student_id, question_id, final_score, decision, reason}]
      Legacy fallback:  [{student_id, revised_score, score_changed, revision_reason}]

    Falls back to zero scores for every student if parsing fails entirely,
    ensuring no students are silently dropped from results.
    """
    # ── Parse raw text → Python object ───────────────────────────────────────
    parsed_obj = None
    try:
        parsed_obj = json.loads(raw)
    except json.JSONDecodeError:
        # Try to fish out the first JSON array or object from the text
        for pattern in (r"\{.*\}", r"\[.*\]"):
            m = re.search(pattern, raw, re.DOTALL)
            if m:
                try:
                    parsed_obj = json.loads(m.group(0))
                    break
                except json.JSONDecodeError:
                    continue

    # ── Extract the results array ─────────────────────────────────────────────
    if isinstance(parsed_obj, dict):
        # Individual template wraps results: {"question_id": "…", "results": […]}
        data = parsed_obj.get("results", [])
    elif isinstance(parsed_obj, list):
        data = parsed_obj
    else:
        data = []

    # ── Build student_id → entry lookup ──────────────────────────────────────
    parsed_by_id: dict[str, dict] = {}
    for entry in data:
        if isinstance(entry, dict):
            sid = str(entry.get("student_id", "")).strip()
            if sid:
                parsed_by_id[sid] = entry

    # ── Map to normalised result dicts ────────────────────────────────────────
    results = []
    for s in student_batch:
        sid   = s["student_id"]
        entry = parsed_by_id.get(sid, {})

        if phase in ("individual", "consensus_initial"):
            # Accept both "assigned_score" (template) and "initial_score" (legacy).
            # Coerce to float immediately so downstream lists are homogeneous float.
            score_raw = (
                entry.get("assigned_score")
                or entry.get("initial_score")
                or 0.0
            )
            # max_score comes from unit["max_score"] which is float, but guard
            # with float() in case a Decimal ever leaks through from the unit dict.
            max_score_f = float(max_score)
            results.append({
                "extraction_id":  s["extraction_id"],
                "student_id":     sid,
                "assigned_score": min(float(score_raw), max_score_f),
                "max_score":      max_score_f,
                "basis_used":     entry.get("basis_used", "answer_key"),
                "reason":         entry.get("reason", ""),
                "matched_points": entry.get("matched_points", []),
                "missing_points": entry.get("missing_points", []),
            })

        else:  # consensus_revised / Phase 2
            # Template output uses "final_score" + "decision"
            # Legacy output used  "revised_score" + "score_changed"
            revised_raw = (
                entry.get("final_score")
                or entry.get("revised_score")
                or entry.get("assigned_score")
                or 0.0
            )
            decision   = entry.get("decision", "")           # "kept" | "revised"
            changed    = (
                (decision == "revised")
                if decision
                else bool(entry.get("score_changed", False))
            )
            reason = (
                entry.get("reason")
                or entry.get("revision_reason")
                or ""
            )
            max_score_f = float(max_score)
            results.append({
                "extraction_id":   s["extraction_id"],
                "student_id":      sid,
                "revised_score":   min(float(revised_raw), max_score_f),
                "max_score":       max_score_f,
                "score_changed":   changed,
                "revision_reason": reason,
            })

    return results


# ══════════════════════════════════════════════════════════════════════════════
#  Individual Mode
# ══════════════════════════════════════════════════════════════════════════════

def _grade_individual_with_model(
    client: GradingModelClient,
    unit: dict,
    student_batch: list[dict],
    rubric_text: Optional[str],
    grading_basis: str,
    general_grading_instructions: Optional[str],
    prompt_builder=None,
) -> list[dict]:
    """
    prompt_builder
        Callable with the same signature as build_individual_prompt().
        Defaults to build_individual_prompt when None.
        Pass build_deepseek_imperfect_prompt when DEEPSEEK_RESEARCH_MODE is
        True and client.name == "deepseek".  No other caller should pass a
        non-default value here — this parameter exists solely to keep the
        DeepSeek research path isolated without any shared-state mutation.
    """
    # Resolve the prompt builder.  Default is the standard individual template.
    # build_deepseek_imperfect_prompt is injected here when model == "deepseek"
    # AND DEEPSEEK_RESEARCH_MODE is True; see the call site in run_grading_background.
    # No other caller should pass a non-None value.
    if prompt_builder is None:
        prompt_builder = build_individual_prompt
    if _question_has_images(unit, student_batch):
        # ── Per-student mode (image question) ───────────────────────────────���
        print(
            f"[IMAGE-GRADING-SKIP-BATCH] question_id={unit['question_id']} "
            f"reason=image_question"
        )
        ak_images        = _collect_answer_key_images_for_unit(unit)
        all_results: list[dict] = []

        for s in student_batch:
            combined = (s.get("images", []) + ak_images) or None

            print(
                f"[IMAGE-GRADING-SCOPE] question_id={unit['question_id']} "
                f"mode=single_student student_id={s['student_id']}"
            )
            single_batch            = [s]
            diagram_context_s, meta_s = _build_diagram_context(unit, single_batch)
            _log_diagram_debug(unit["question_id"], meta_s, phase="individual_per_student")

            prompt = prompt_builder(
                question_id                  = unit["question_id"],
                question_text                = unit["question_text"],
                answer_type                  = unit.get("answer_type", "recall_general"),
                max_score                    = unit["max_score"],
                grading_basis                = grading_basis,
                rubric_text                  = rubric_text,
                answer_key_text              = unit["answer_key_text"],
                general_grading_instructions = general_grading_instructions,
                scoring_method               = unit.get("scoring_method"),
                additional_checks            = unit.get("additional_checks"),
                custom_instructions          = unit.get("custom_instructions"),
                diagram_context              = diagram_context_s,
                student_answers              = [
                    {
                        "student_id":          s["student_id"],
                        "student_name":        s["student_name"],
                        "answer":              s["answer"],
                        "drawing_description": s.get("drawing_description"),
                        "has_drawing":         s.get("has_drawing", False),
                    }
                ],
            )
            print(
                f"[grading] sending to {client.name} for question {unit['question_id']}"
                f" student={s['student_id']}"
                + (f" [{len(combined)} image(s) attached]" if combined else "")
            )
            raw = _timed_complete(client, prompt, combined, unit["question_id"], "individual")
            print(
                f"[grading] received {client.name} response for question "
                f"{unit['question_id']} student={s['student_id']}"
            )
            results = _parse_grading_response(raw, single_batch, unit["max_score"], "individual")
            for r in results:
                r["model_name"] = client.name
                r["phase"]      = GradingPhase.individual
            all_results.extend(results)

        return all_results

    # ── Batch mode (text-only question) — original behavior, unchanged ────────
    images = _collect_unit_images(unit, student_batch) or None
    diagram_context, diagram_meta = _build_diagram_context(unit, student_batch)
    _log_diagram_debug(unit["question_id"], diagram_meta, phase="individual")

    prompt = prompt_builder(
        question_id                  = unit["question_id"],
        question_text                = unit["question_text"],
        answer_type                  = unit.get("answer_type", "recall_general"),
        max_score                    = unit["max_score"],
        grading_basis                = grading_basis,
        rubric_text                  = rubric_text,
        answer_key_text              = unit["answer_key_text"],
        general_grading_instructions = general_grading_instructions,
        scoring_method               = unit.get("scoring_method"),
        additional_checks            = unit.get("additional_checks"),
        custom_instructions          = unit.get("custom_instructions"),
        diagram_context              = diagram_context,
        student_answers              = [
            {
                "student_id":          s["student_id"],
                "student_name":        s["student_name"],
                "answer":              s["answer"],
                "drawing_description": s.get("drawing_description"),
                "has_drawing":         s.get("has_drawing", False),
            }
            for s in student_batch
        ],
    )
    print(
        f"[grading] sending to {client.name} for question {unit['question_id']}"
        + (f" [{len(images)} image(s) attached]" if images else "")
    )
    raw = _timed_complete(client, prompt, images, unit["question_id"], "individual")
    print(f"[grading] received {client.name} response for question {unit['question_id']}")
    results = _parse_grading_response(raw, student_batch, unit["max_score"], "individual")
    for r in results:
        r["model_name"] = client.name
        r["phase"]      = GradingPhase.individual
    return results


# ══════════════════════════════════════════════════════════════════════════════
#  Multimodal image helpers
# ══════════════════════════════════════════════════════════════════════════════

_IMAGE_EXTENSIONS: dict[str, str] = {
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png":  "image/png",
    ".gif":  "image/gif",
    ".webp": "image/webp",
    ".bmp":  "image/bmp",
}


def _load_image_as_base64(file_path: str) -> Optional[dict]:
    """
    Read an image file from disk and return a base64-encoded image dict.

    Returns
    -------
    dict | None
        {"data": "<base64>", "media_type": "image/jpeg", "_path": "<file_path>"}
        or None if the file does not exist or cannot be read.
    """
    if not file_path:
        return None
    if not os.path.exists(file_path):
        print(f"[grading-engine] WARN: image not found at {file_path!r}")
        return None
    try:
        mime, _ = mimetypes.guess_type(file_path)
        if not mime or not mime.startswith("image/"):
            ext  = os.path.splitext(file_path)[1].lower()
            mime = _IMAGE_EXTENSIONS.get(ext, "image/jpeg")
        with open(file_path, "rb") as fh:
            encoded = base64.standard_b64encode(fh.read()).decode("utf-8")
        return {"data": encoded, "media_type": mime, "_path": file_path}
    except Exception as exc:
        print(f"[grading-engine] WARN: could not load image {file_path!r}: {exc}")
        return None


def _extract_answer_key_images(answer_key_text: str) -> list[dict]:
    """
    Try to locate image files referenced in the answer key text.

    Handles two cases:
      1. JSON answer key with an "image_path", "diagram_path", or "figure_path" key.
      2. A plain string that looks like a file path ending in an image extension.

    Returns a (possibly empty) list of base64 image dicts.
    """
    if not answer_key_text:
        return []
    images: list[dict] = []
    # Case 1: structured JSON
    if answer_key_text.strip().startswith("{"):
        try:
            parsed = json.loads(answer_key_text)
            if isinstance(parsed, dict):
                for key in ("image_path", "diagram_path", "figure_path"):
                    path = parsed.get(key)
                    if path:
                        img = _load_image_as_base64(path)
                        if img:
                            images.append(img)
        except (json.JSONDecodeError, TypeError):
            pass
    # Case 2: plain path string
    elif any(answer_key_text.strip().lower().endswith(ext) for ext in _IMAGE_EXTENSIONS):
        img = _load_image_as_base64(answer_key_text.strip())
        if img:
            images.append(img)
    return images


def _build_diagram_context(
    unit: dict,
    student_batch: list[dict],
) -> tuple[Optional[str], dict]:
    """
    Build the DIAGRAM_CONTEXT string injected into the grading prompt and a
    metadata dict used for debug logging.

    Returns
    -------
    (context_str, meta)
    context_str : str | None
        Human-readable block describing which images are attached and how the
        model should use them.  None when no images are present (the optional
        {{#DIAGRAM_CONTEXT}} block is then omitted from the prompt entirely).
    meta : dict
        {
          "student_drawing_count": int,   # students with a loaded drawing image
          "ak_image_count":        int,   # answer key images
          "total_images":          int,
        }
    """
    # Count students who have a successfully loaded drawing image
    student_drawing_count = sum(
        1 for s in student_batch
        if s.get("has_drawing") and s.get("images")
    )
    # Count answer key diagram images (parses the answer_key_text for image paths)
    ak_imgs = _extract_answer_key_images(unit.get("answer_key_text", ""))
    ak_image_count = len(ak_imgs)
    total_images = student_drawing_count + ak_image_count

    meta = {
        "student_drawing_count": student_drawing_count,
        "ak_image_count":        ak_image_count,
        "total_images":          total_images,
    }

    if total_images == 0:
        return None, meta

    lines: list[str] = [
        f"{total_images} image(s) are attached to this grading request in the following order:",
    ]

    if student_drawing_count > 0:
        end_idx = student_drawing_count
        lines.append(
            f"  • Image(s) 1–{end_idx}: Student diagram(s) — one image per student, "
            f"in the same order as the student batch below."
        )

    if ak_image_count > 0:
        ak_start = student_drawing_count + 1
        ak_end   = total_images
        lbl = f"Image {ak_start}" if ak_image_count == 1 else f"Images {ak_start}–{ak_end}"
        lines.append(
            f"  • {lbl}: Answer key reference diagram(s) — the expected/correct visual solution."
        )
        lines += [
            "",
            "Visual grading instruction:",
            "  A diagram from the student answer is attached.",
            "  A diagram from the answer key is also attached.",
            "  Compare both visually and grade accordingly.",
            "  Assess: structural correctness, labels, arrows, flow direction, "
            "relationships, and all required components.",
            "  A student diagram is correct when it matches the answer key diagram's "
            "structure and content — exact layout is not required.",
        ]
    else:
        # Student drawings only, no answer key image
        lines += [
            "",
            "Visual grading instruction:",
            "  A diagram from the student answer is attached.",
            "  Evaluate the student's diagram against the answer key text and grading policy above.",
            "  Assess: structural correctness, labels, arrows, flow direction, "
            "and all required components.",
        ]

    return "\n".join(lines), meta


def _log_diagram_debug(question_id: str, meta: dict, phase: str = "") -> None:
    """Emit a [diagram-debug] and [diagram-images-sent] log line for one question grading call."""
    phase_tag = f" [{phase}]" if phase else ""
    print(
        f"[diagram-debug]{phase_tag} "
        f"question_id={question_id} "
        f"student_image={'True' if meta['student_drawing_count'] > 0 else 'False'} "
        f"answer_key_image={'True' if meta['ak_image_count'] > 0 else 'False'} "
        f"images_sent={meta['total_images']}"
    )
    print(
        f"[diagram-images-sent] "
        f"question_id={question_id} "
        f"student_image={'True' if meta['student_drawing_count'] > 0 else 'False'} "
        f"answer_key_image={'True' if meta['ak_image_count'] > 0 else 'False'} "
        f"images_sent={meta['total_images']}"
    )


def _collect_unit_images(unit: dict, student_batch: list[dict]) -> list[dict]:
    """
    Aggregate all images for one grading unit to attach to the LLM call.

    Delivery order (guaranteed):
      1. Student drawings   — StudentAnswerItem.drawing_file_path  (has_drawing=True)
      2. Answer key images  — embedded path(s) in unit["answer_key_text"]  (2a)
                           — paths from unit["answer_key_images_column"]   (2b, supplement)

    Deduplication: each unique file path is included at most once.

    Returns a (possibly empty) list of base64 image dicts ready for
    GradingModelClient.complete(images=...).
    """
    qid    = unit.get("question_id", "?")
    images: list[dict] = []
    seen_paths: set[str] = set()
    ak_attached = False

    # ── 1. Student drawings (capped at _MAX_STUDENT_IMAGES per call) ─────────
    # With many students all submitting diagram answers, sending all drawings
    # in a single multimodal payload can cause API stalls or timeouts.
    # We cap at _MAX_STUDENT_IMAGES; the grading prompt includes a text
    # drawing_description for every student so the model can still assess
    # diagrams for students whose image was not attached.
    total_with_drawings = sum(
        1 for s in student_batch
        if s.get("has_drawing") and s.get("images")
    )
    student_imgs_added = 0
    for s in student_batch:
        if student_imgs_added >= _MAX_STUDENT_IMAGES:
            break
        for img in s.get("images", []):
            p = img.get("_path", "")
            if p and p not in seen_paths:
                images.append(img)
                seen_paths.add(p)
                student_imgs_added += 1
                break  # at most one drawing image per student

    if total_with_drawings > _MAX_STUDENT_IMAGES:
        omitted = total_with_drawings - student_imgs_added
        print(
            f"[IMAGE-CAP] question_id={qid} "
            f"images_included={student_imgs_added} "
            f"total_with_drawings={total_with_drawings} "
            f"cap={_MAX_STUDENT_IMAGES} "
            f"({omitted} student diagram(s) omitted from image payload — "
            f"text drawing_description still included in prompt)"
        )

    # ── 2a. Answer key images embedded in model_answer text ──────────────────
    for img in _extract_answer_key_images(unit.get("answer_key_text", "")):
        p = img.get("_path", "")
        if p and p not in seen_paths:
            images.append(img)
            seen_paths.add(p)
            ak_attached = True
            print(f"[DIAGRAM] Answer key diagram attached from model_answer: {p}")

    # ── 2b. Answer key images from answer_key_images DB column (supplement) ──
    for path in (unit.get("answer_key_images_column") or []):
        if path and path not in seen_paths:
            img = _load_image_as_base64(path)
            if img:
                images.append(img)
                seen_paths.add(path)
                ak_attached = True
                print(f"[DIAGRAM] Answer key diagram attached from answer_key_images column: {path}")

    # ── Warn when a diagram question has no answer key reference image ────────
    if not ak_attached and unit.get("answer_type") == "diagrammatic_aoa":
        print(f"[DIAGRAM] Answer key diagram missing for question {qid}")

    return images


def _collect_answer_key_images_for_unit(unit: dict) -> list[dict]:
    """
    Return only the answer-key images for one unit — no student drawings.

    Used by per-student image mode so the same AK images can be combined
    with each individual student's drawing without re-loading from disk
    on every iteration.

    Mirrors the AK-image collection steps inside _collect_unit_images:
      2a. Images embedded in unit["answer_key_text"]
      2b. Images from unit["answer_key_images_column"]

    Returns a (possibly empty) list of base64 image dicts.
    """
    ak_images: list[dict] = []
    seen_paths: set[str]  = set()

    for img in _extract_answer_key_images(unit.get("answer_key_text", "")):
        p = img.get("_path", "")
        if p and p not in seen_paths:
            ak_images.append(img)
            seen_paths.add(p)

    for path in (unit.get("answer_key_images_column") or []):
        if path and path not in seen_paths:
            img = _load_image_as_base64(path)
            if img:
                ak_images.append(img)
                seen_paths.add(path)

    return ak_images


def _question_has_images(unit: dict, student_batch: list[dict]) -> bool:
    """
    Return True if this question involves any images at all.

    Checks (in-memory only — no disk I/O):
      • Any student in the batch has a drawing image already loaded
      • The unit references answer-key images via the DB column
      • The unit's answer_key_text contains a JSON image-path field
        ("image_path", "diagram_path", "figure_path")
      • The unit's answer_key_text is itself a plain image-file path

    This is the gating test used by individual and consensus grading to
    decide between batch mode (all students in one call, no images) and
    per-student mode (one call per student with that student's images).
    """
    # Student drawing images already loaded by _get_student_answers_for_unit
    if any(s.get("has_drawing") and s.get("images") for s in student_batch):
        return True
    # Answer-key image paths in DB column
    if unit.get("answer_key_images_column"):
        return True
    # Answer-key image embedded in model_answer JSON
    ak_text = unit.get("answer_key_text", "")
    if ak_text.strip().startswith("{"):
        try:
            parsed = json.loads(ak_text)
            if isinstance(parsed, dict):
                if any(parsed.get(k) for k in ("image_path", "diagram_path", "figure_path")):
                    return True
        except (json.JSONDecodeError, TypeError):
            pass
    elif any(ak_text.strip().lower().endswith(ext) for ext in _IMAGE_EXTENSIONS):
        return True
    return False


# ══════════════════════════════════════════════════════════════════════════════
#  Consensus Mode
# ══════════════════════════════════════════════════════════════════════════════

_EXAMINER_LABELS = ["Examiner 1", "Examiner 2"]   # Claude, OpenAI — consensus only


def _grade_consensus(
    unit: dict,
    student_batch: list[dict],
    rubric_text: Optional[str],
    grading_basis: str,
    general_grading_instructions: Optional[str],
) -> dict:
    """
    Run two-model consensus grading for one question unit.

    Examiners: Claude (Examiner 1) and OpenAI (Examiner 2).

    Phase 1  — both models grade independently with multimodal support.
    Phase 2  — discussion round, runs ONLY when the two models disagree
               on at least one student's score for this question.
               Questions where all students received identical scores from
               both models skip Phase 2 entirely.

    Returns a dict with:
      initial_results — list of per-model per-student phase-1 results
      revised_results — list of per-model per-student phase-2 results (may be [])
      final_scores    — {student_id: final_score}

    Final score rules:
      • Agreed question (no phase 2): use the common phase-1 score.
      • Disagreed question (phase 2 ran): average the two revised scores.
    """
    clients = all_clients()   # [ClaudeGradingClient, OpenAIGradingClient]

    # Determine whether this question involves any images.
    # When True, both phases switch to per-student mode (one call per student)
    # so each request carries at most one drawing + the shared AK images.
    has_images = _question_has_images(unit, student_batch)
    # Pre-load AK images once; reused for every student in per-student mode.
    ak_images  = _collect_answer_key_images_for_unit(unit) if has_images else []

    # Collect all images for batch mode (used when has_images is False).
    images = _collect_unit_images(unit, student_batch) or None

    # Build diagram context string (also used for debug logging)
    diagram_context, diagram_meta = _build_diagram_context(unit, student_batch)
    _log_diagram_debug(unit["question_id"], diagram_meta, phase="consensus")

    if images and not has_images:
        print(
            f"[grading] {len(images)} image(s) attached for question "
            f"{unit['question_id']} (consensus)"
        )

    # Shared student_answers list (batch mode only)
    student_answers_list = [
        {
            "student_id":          s["student_id"],
            "student_name":        s["student_name"],
            "answer":              s["answer"],
            "drawing_description": s.get("drawing_description"),
            "has_drawing":         s.get("has_drawing", False),
        }
        for s in student_batch
    ]

    # ── Phase 1: both models grade independently ──────────────────────────────
    phase1_results: dict[str, list[dict]] = {}  # examiner_label → results

    def _zero_results(label: str, phase) -> list[dict]:
        """Fallback zero-score result list for a timed-out / errored examiner."""
        return [
            {
                "extraction_id":  s["extraction_id"],
                "student_id":     s["student_id"],
                "assigned_score": 0.0,
                "max_score":      float(unit["max_score"]),
                "basis_used":     "answer_key",
                "reason":         "[TIMEOUT] Model did not respond within the time limit.",
                "matched_points": [],
                "missing_points": [],
                "model_name":     "timeout",
                "examiner_label": label,
                "phase":          phase,
            }
            for s in student_batch
        ]

    def _run_phase1(client: GradingModelClient, label: str) -> tuple[str, list[dict]]:
        if has_images:
            # ── Per-student mode (image question) ─────────────────────��──────
            # One call per student: this student's drawing + shared AK images.
            print(
                f"[IMAGE-GRADING-SKIP-BATCH] question_id={unit['question_id']} "
                f"reason=image_question"
            )
            all_ph1: list[dict] = []
            for s in student_batch:
                combined = (s.get("images", []) + ak_images) or None
                print(
                    f"[IMAGE-GRADING-SCOPE] question_id={unit['question_id']} "
                    f"mode=single_student student_id={s['student_id']}"
                )
                single_batch  = [s]
                _dc_s, _      = _build_diagram_context(unit, single_batch)
                prompt = build_consensus_phase1_prompt(
                    examiner_label               = label,
                    question_id                  = unit["question_id"],
                    question_text                = unit["question_text"],
                    answer_type                  = unit.get("answer_type", "recall_general"),
                    max_score                    = unit["max_score"],
                    grading_basis                = grading_basis,
                    rubric_text                  = rubric_text,
                    answer_key_text              = unit["answer_key_text"],
                    general_grading_instructions = general_grading_instructions,
                    scoring_method               = unit.get("scoring_method"),
                    additional_checks            = unit.get("additional_checks"),
                    custom_instructions          = unit.get("custom_instructions"),
                    diagram_context              = _dc_s,
                    student_answers              = [
                        {
                            "student_id":          s["student_id"],
                            "student_name":        s["student_name"],
                            "answer":              s["answer"],
                            "drawing_description": s.get("drawing_description"),
                            "has_drawing":         s.get("has_drawing", False),
                        }
                    ],
                )
                print(
                    f"[grading] sending to {client.name} for question "
                    f"{unit['question_id']} student={s['student_id']} (consensus phase 1)"
                )
                raw = _timed_complete(client, prompt, combined, unit["question_id"], "consensus_phase1")
                print(
                    f"[grading] received {client.name} response for question "
                    f"{unit['question_id']} student={s['student_id']} (consensus phase 1)"
                )
                results = _parse_grading_response(raw, single_batch, unit["max_score"], "consensus_initial")
                for r in results:
                    r["model_name"]     = client.name
                    r["examiner_label"] = label
                    r["phase"]          = GradingPhase.consensus_initial
                all_ph1.extend(results)
            return label, all_ph1

        # ── Batch mode (text-only question) — original behavior unchanged ────
        prompt = build_consensus_phase1_prompt(
            examiner_label               = label,
            question_id                  = unit["question_id"],
            question_text                = unit["question_text"],
            answer_type                  = unit.get("answer_type", "recall_general"),
            max_score                    = unit["max_score"],
            grading_basis                = grading_basis,
            rubric_text                  = rubric_text,
            answer_key_text              = unit["answer_key_text"],
            general_grading_instructions = general_grading_instructions,
            scoring_method               = unit.get("scoring_method"),
            additional_checks            = unit.get("additional_checks"),
            custom_instructions          = unit.get("custom_instructions"),
            diagram_context              = diagram_context,
            student_answers              = student_answers_list,
        )
        print(
            f"[grading] sending to {client.name} for question "
            f"{unit['question_id']} (consensus phase 1)"
        )
        raw = _timed_complete(client, prompt, images, unit["question_id"], "consensus_phase1")
        print(f"[grading] received {client.name} response (consensus phase 1)")
        results = _parse_grading_response(
            raw, student_batch, unit["max_score"], "consensus_initial"
        )
        for r in results:
            r["model_name"]     = client.name
            r["examiner_label"] = label
            r["phase"]          = GradingPhase.consensus_initial
        return label, results

    print(f"[grading] consensus phase 1 started for question {unit['question_id']}")
    # Use cf.wait with _PHASE_TIMEOUT_SECS so the entire phase has a hard ceiling.
    # shutdown(wait=False) releases the pool without blocking on hung threads.
    _ph1_pool = ThreadPoolExecutor(max_workers=len(clients))
    try:
        _ph1_future_map = {
            _ph1_pool.submit(_run_phase1, client, label): (client.name, label)
            for client, label in zip(clients, _EXAMINER_LABELS)
        }
        _ph1_done, _ph1_not_done = _cf.wait(
            list(_ph1_future_map.keys()),
            timeout=_PHASE_TIMEOUT_SECS,
        )

        for future in _ph1_not_done:
            _mname, _lbl = _ph1_future_map[future]
            print(
                f"[LLM-CALL-TIMEOUT] phase=consensus_phase1 "
                f"model={_mname} label={_lbl} "
                f"timeout={_PHASE_TIMEOUT_SECS}s — using zero-score fallback"
            )
            phase1_results[_lbl] = _zero_results(_lbl, GradingPhase.consensus_initial)

        for future in _ph1_done:
            _mname, _lbl = _ph1_future_map[future]
            try:
                returned_lbl, results = future.result()
                phase1_results[returned_lbl] = results
            except Exception as _exc:
                print(
                    f"[LLM-CALL-ERROR] phase=consensus_phase1 "
                    f"model={_mname} label={_lbl} error={_exc}"
                )
                phase1_results[_lbl] = _zero_results(_lbl, GradingPhase.consensus_initial)
    finally:
        _ph1_pool.shutdown(wait=False)  # don't block on any still-running thread

    print(f"[grading] consensus phase 1 completed for question {unit['question_id']}")

    # Build lookup: {student_id: {examiner_label: {"score": X, "reason": "..."}}}
    all_initial_scores: dict[str, dict] = {}
    for label, results in phase1_results.items():
        for r in results:
            sid = r["student_id"]
            if sid not in all_initial_scores:
                all_initial_scores[sid] = {}
            all_initial_scores[sid][label] = {
                "score":  r["assigned_score"],
                "reason": r.get("reason", ""),
            }

    # ── Disagreement check ────────────────────────────────────────────────────
    # For each student, compare the two examiners' phase-1 scores.
    # If ANY student has a score mismatch → run phase 2 for the whole question.
    # If ALL students received identical scores → skip phase 2 entirely.
    has_disagreement = False
    for s in student_batch:
        sid = s["student_id"]
        scores_this_student = [
            all_initial_scores.get(sid, {}).get(lbl, {}).get("score", 0.0)
            for lbl in _EXAMINER_LABELS
        ]
        if len(set(scores_this_student)) > 1:
            has_disagreement = True
            break

    # ── Phase 2 (conditional): discussion round on disagreement ───────────────
    phase2_results: dict[str, list[dict]] = {}

    if not has_disagreement:
        print(
            f"[grading] all students agreed for question {unit['question_id']} "
            f"— skipping phase 2 discussion"
        )
        print(f"[consensus-skipped] question_id={unit['question_id']} reason=models_agreed")
    else:
        print(
            f"[grading] score disagreement detected for question {unit['question_id']} "
            f"— running phase 2 discussion"
        )
        # Log the first disagreeing student's scores for traceability
        for _s in student_batch:
            _sid = _s["student_id"]
            _sc = {
                lbl: all_initial_scores.get(_sid, {}).get(lbl, {}).get("score", 0.0)
                for lbl in _EXAMINER_LABELS
            }
            if len(set(_sc.values())) > 1:
                print(
                    f"[consensus-triggered] question_id={unit['question_id']} "
                    f"model_A_score={_sc[_EXAMINER_LABELS[0]]} "
                    f"model_B_score={_sc[_EXAMINER_LABELS[1]]}"
                )
                break

        def _run_phase2(client: GradingModelClient, label: str) -> tuple[str, list[dict]]:
            if has_images:
                # ── Per-student mode (image question) ──────────────────���─────
                # One call per student.  all_initial_scores (all students) is
                # kept in the prompt for examiner context; student_answers is
                # narrowed to one student so the image payload stays small.
                all_ph2: list[dict] = []
                for s in student_batch:
                    combined = (s.get("images", []) + ak_images) or None
                    print(
                        f"[IMAGE-GRADING-SCOPE] question_id={unit['question_id']} "
                        f"mode=single_student student_id={s['student_id']}"
                    )
                    single_batch = [s]
                    _dc_s, _     = _build_diagram_context(unit, single_batch)
                    prompt = build_consensus_phase2_prompt(
                        examiner_label               = label,
                        question_id                  = unit["question_id"],
                        question_text                = unit["question_text"],
                        answer_type                  = unit.get("answer_type", "recall_general"),
                        max_score                    = unit["max_score"],
                        grading_basis                = grading_basis,
                        rubric_text                  = rubric_text,
                        answer_key_text              = unit["answer_key_text"],
                        general_grading_instructions = general_grading_instructions,
                        scoring_method               = unit.get("scoring_method"),
                        additional_checks            = unit.get("additional_checks"),
                        custom_instructions          = unit.get("custom_instructions"),
                        diagram_context              = _dc_s,
                        student_answers              = [
                            {
                                "student_id":          s["student_id"],
                                "student_name":        s["student_name"],
                                "answer":              s["answer"],
                                "drawing_description": s.get("drawing_description"),
                                "has_drawing":         s.get("has_drawing", False),
                            }
                        ],
                        all_initial_scores           = all_initial_scores,
                    )
                    print(
                        f"[grading] sending to {client.name} for question "
                        f"{unit['question_id']} student={s['student_id']} (consensus phase 2)"
                    )
                    raw = _timed_complete(client, prompt, combined, unit["question_id"], "consensus_phase2")
                    print(
                        f"[grading] received {client.name} response for question "
                        f"{unit['question_id']} student={s['student_id']} (consensus phase 2)"
                    )
                    results = _parse_grading_response(raw, single_batch, unit["max_score"], "consensus_revised")
                    for r in results:
                        r["model_name"]     = client.name
                        r["examiner_label"] = label
                        r["phase"]          = GradingPhase.consensus_revised
                    all_ph2.extend(results)
                return label, all_ph2

            # ── Batch mode (text-only question) — original behavior unchanged ──
            prompt = build_consensus_phase2_prompt(
                examiner_label               = label,
                question_id                  = unit["question_id"],
                question_text                = unit["question_text"],
                answer_type                  = unit.get("answer_type", "recall_general"),
                max_score                    = unit["max_score"],
                grading_basis                = grading_basis,
                rubric_text                  = rubric_text,
                answer_key_text              = unit["answer_key_text"],
                general_grading_instructions = general_grading_instructions,
                scoring_method               = unit.get("scoring_method"),
                additional_checks            = unit.get("additional_checks"),
                custom_instructions          = unit.get("custom_instructions"),
                diagram_context              = diagram_context,
                student_answers              = student_answers_list,
                all_initial_scores           = all_initial_scores,
            )
            print(
                f"[grading] sending to {client.name} for question "
                f"{unit['question_id']} (consensus phase 2)"
            )
            raw = _timed_complete(client, prompt, images, unit["question_id"], "consensus_phase2")
            print(f"[grading] received {client.name} response (consensus phase 2)")
            results = _parse_grading_response(
                raw, student_batch, unit["max_score"], "consensus_revised"
            )
            for r in results:
                r["model_name"]     = client.name
                r["examiner_label"] = label
                r["phase"]          = GradingPhase.consensus_revised
            return label, results

        print(f"[grading] consensus phase 2 started for question {unit['question_id']}")
        _ph2_pool = ThreadPoolExecutor(max_workers=len(clients))
        try:
            _ph2_future_map = {
                _ph2_pool.submit(_run_phase2, client, label): (client.name, label)
                for client, label in zip(clients, _EXAMINER_LABELS)
            }
            _ph2_done, _ph2_not_done = _cf.wait(
                list(_ph2_future_map.keys()),
                timeout=_PHASE_TIMEOUT_SECS,
            )

            for future in _ph2_not_done:
                _mname, _lbl = _ph2_future_map[future]
                print(
                    f"[LLM-CALL-TIMEOUT] phase=consensus_phase2 "
                    f"model={_mname} label={_lbl} "
                    f"timeout={_PHASE_TIMEOUT_SECS}s — using zero-score fallback"
                )
                phase2_results[_lbl] = [
                    {
                        "extraction_id":   s["extraction_id"],
                        "student_id":      s["student_id"],
                        "revised_score":   0.0,
                        "max_score":       float(unit["max_score"]),
                        "score_changed":   False,
                        "revision_reason": "[TIMEOUT] Model did not respond.",
                        "model_name":      "timeout",
                        "examiner_label":  _lbl,
                        "phase":           GradingPhase.consensus_revised,
                    }
                    for s in student_batch
                ]

            for future in _ph2_done:
                _mname, _lbl = _ph2_future_map[future]
                try:
                    returned_lbl, results = future.result()
                    phase2_results[returned_lbl] = results
                except Exception as _exc:
                    print(
                        f"[LLM-CALL-ERROR] phase=consensus_phase2 "
                        f"model={_mname} label={_lbl} error={_exc}"
                    )
                    phase2_results[_lbl] = [
                        {
                            "extraction_id":   s["extraction_id"],
                            "student_id":      s["student_id"],
                            "revised_score":   0.0,
                            "max_score":       float(unit["max_score"]),
                            "score_changed":   False,
                            "revision_reason": f"[ERROR] {str(_exc)[:200]}",
                            "model_name":      "error",
                            "examiner_label":  _lbl,
                            "phase":           GradingPhase.consensus_revised,
                        }
                        for s in student_batch
                    ]
        finally:
            _ph2_pool.shutdown(wait=False)  # don't block on hung threads

        print(f"[grading] consensus phase 2 completed for question {unit['question_id']}")

    # ── Compute final scores ──────────────────────────────────────────────────
    final_scores: dict[str, float] = {}

    max_score = unit["max_score"]

    if not has_disagreement:
        # All agreed in phase 1 → use the common phase-1 score directly
        for s in student_batch:
            sid = s["student_id"]
            agreed_score = next(
                (
                    r["assigned_score"]
                    for r in phase1_results.get(_EXAMINER_LABELS[0], [])
                    if r["student_id"] == sid
                ),
                0.0,
            )
            final = _round_quarter(agreed_score, max_score)
            print(
                f"[ROUNDING] mode=consensus_agreed "
                f"question_id={unit['question_id']} student_id={sid} "
                f"raw={agreed_score:.4f} → final={final}"
            )
            final_scores[sid] = final
    else:
        # Phase 2 ran → average the two revised scores.
        # Coerce every revised_score to float so sum() stays in float-land
        # (avoids Decimal + float if max_score ever leaked as Decimal).
        for s in student_batch:
            sid = s["student_id"]
            revised: list[float] = []
            for label, results in phase2_results.items():
                for r in results:
                    if r["student_id"] == sid:
                        revised.append(float(r["revised_score"]))
                        break
            raw_avg = sum(revised) / len(revised) if revised else 0.0
            final = _round_quarter(raw_avg, max_score)
            print(
                f"[ROUNDING] mode=consensus_revised "
                f"question_id={unit['question_id']} student_id={sid} "
                f"raw={raw_avg:.4f} → final={final}"
            )
            final_scores[sid] = final

    return {
        "initial_results": [r for results in phase1_results.values() for r in results],
        "revised_results": [r for results in phase2_results.values() for r in results],
        "final_scores":    final_scores,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  Result storage helpers
# ══════════════════════════════════════════════════════════════════════════════

def _store_model_grading_results(
    db: Session,
    job_id: int,
    question_id: str,
    results: list[dict],
) -> None:
    """Persist a batch of per-model results to model_grading_results."""
    for r in results:
        # These Numeric columns accept Decimal or float; use float() to stay
        # consistent with what the LLM pipeline produces.
        raw_score = r.get("assigned_score") or r.get("revised_score") or 0.0
        db.add(ModelGradingResult(
            grading_job_id  = job_id,
            extraction_id   = r["extraction_id"],
            question_id     = question_id,
            model_name      = r.get("model_name", "deterministic"),
            examiner_label  = r.get("examiner_label"),
            phase           = r.get("phase", GradingPhase.individual),
            assigned_score  = float(raw_score),
            max_score       = float(r["max_score"]),
            basis_used      = r.get("basis_used", "answer_key"),
            reason          = r.get("reason") or r.get("revision_reason", ""),
            matched_points  = json.dumps(r.get("matched_points", []), ensure_ascii=False),
            missing_points  = json.dumps(r.get("missing_points", []), ensure_ascii=False),
            score_changed   = r.get("score_changed"),
            revision_reason = r.get("revision_reason"),
        ))


def _store_final_score(
    db: Session,
    job_id: int,
    extraction_id: int,
    question_id: str,
    final_score,        # float, Decimal, or int accepted; coerced to float internally
    max_score,          # float, Decimal, or int accepted; coerced to float internally
    grading_mode: str,
    reasoning_summary: str,
    matched_points: list,
    missing_points: list,
) -> None:
    # Normalise to float so _round_quarter and GradingFinalScore are consistent.
    final_score = float(final_score)
    max_score   = float(max_score)
    rounded = _round_quarter(final_score, max_score)
    db.add(GradingFinalScore(
        grading_job_id    = job_id,
        extraction_id     = extraction_id,
        question_id       = question_id,
        final_score       = rounded,
        max_score         = max_score,
        grading_mode      = grading_mode,
        reasoning_summary = reasoning_summary,
        matched_points    = json.dumps(matched_points, ensure_ascii=False),
        missing_points    = json.dumps(missing_points, ensure_ascii=False),
    ))
    print(
        f"[FINAL-SCORE-WRITE] question_id={question_id} "
        f"student_id={extraction_id} score={rounded}/{max_score} mode={grading_mode}"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Exam results creation
# ══════════════════════════════════════════════════════════════════════════════

def _letter_grade(score, max_score) -> str:
    # Accept Decimal, float, or int — normalise via D() for consistent division
    s  = D(score)
    mx = D(max_score)
    if mx <= 0:
        return "N/A"
    pct = s / mx * 100
    if pct >= 90:
        return "A"
    if pct >= 80:
        return "B"
    if pct >= 70:
        return "C"
    if pct >= 60:
        return "D"
    return "F"


def _compute_avg_from_final_scores(
    job_id: int,
    extractions: "list[dict]",
    db,
) -> "float | None":
    """
    Compute the average total score for the *extractions* batch from the
    already-written GradingFinalScore rows, WITHOUT reading exam.avg_score.

    Used to populate GradingReport.average_score before _create_exam_results
    has had a chance to update the exam aggregate (i.e. in the
    awaiting_confirmation branch where results have not yet been applied).

    Returns a float rounded to 2 dp, or None if no data is available.
    """
    rows = db.query(GradingFinalScore).filter(
        GradingFinalScore.grading_job_id == job_id
    ).all()
    if not rows or not extractions:
        return None

    target_eids = {e["extraction_id"] for e in extractions}
    scores_by_eid: "dict[int, Decimal]" = {}
    for fs in rows:
        if fs.extraction_id not in target_eids:
            continue
        eid = fs.extraction_id
        if eid not in scores_by_eid:
            scores_by_eid[eid] = Decimal("0")
        if fs.final_score is not None:
            scores_by_eid[eid] += D(fs.final_score)

    totals = list(scores_by_eid.values())
    if not totals:
        return None
    avg = sum(totals, Decimal("0")) / len(totals)
    return float(round(avg, 2))


def _create_grading_report(
    db,
    exam_id: int,
    job_id: int,
    student_ids: "list[int] | None",
    student_count: int,
    avg_score: "float | None" = None,
    force_draft: bool = False,
) -> "GradingReport":
    """
    Create a GradingReport record for a completed (or paused) grading job.

    version_number  — auto-incremented per exam (max existing + 1).
    force_draft=True — always create as "draft" (used when confirmation is
                       needed; the report is visible and downloadable
                       immediately, but grades are not yet applied).
    force_draft=False — "active" if no prior active report exists for this
                        exam; "draft" otherwise (caller must activate it
                        after applying results and marking old one replaced).

    avg_score — caller-supplied average; if None the report is left with
                NULL average (updated later by apply_job_results).
    """
    from sqlalchemy import func as _func
    max_ver = db.query(_func.max(GradingReport.version_number)).filter(
        GradingReport.exam_id == exam_id
    ).scalar() or 0
    version = max_ver + 1

    scope = "selected" if student_ids else "all"

    if force_draft:
        # Regrading confirmation path: always draft until instructor approves
        status    = "draft"
        is_active = False
        approved  = None
    else:
        # Normal path: active if no other active report exists
        has_active = db.query(GradingReport).filter(
            GradingReport.exam_id   == exam_id,
            GradingReport.is_active == True,
        ).first() is not None
        if has_active:
            status    = "draft"
            is_active = False
            approved  = None
        else:
            status    = "active"
            is_active = True
            approved  = datetime.utcnow()

    rpt = GradingReport(
        exam_id        = exam_id,
        grading_job_id = job_id,
        version_number = version,
        scope          = scope,
        student_count  = student_count,
        average_score  = avg_score,
        status         = status,
        is_active      = is_active,
        approved_at    = approved,
    )
    db.add(rpt)
    db.flush()
    print(
        f"[report] created v{version} status={status} "
        f"scope={scope} students={student_count} avg={avg_score}"
    )
    return rpt


def _create_exam_results(
    db: Session,
    exam_id: int,
    extractions: list[dict],
    question_units: list[dict],  # [{question_id, question_text, max_score}, ...]
    all_final_scores: dict[int, dict],  # {extraction_id: {question_id: {score, reasoning, matched, missing}}}
    selective: bool = False,
) -> None:
    """
    Upsert ExamResult + StudentQuestionScore rows from the computed final scores.

    selective=False (default / all-students mode):
      Delete all prior results for the exam, then recreate from scratch.

    selective=True (selected-students mode):
      Only delete+recreate rows for the students in `extractions`.
      Existing rows for other students are left untouched.

    All score arithmetic is done exclusively in decimal.Decimal (via D()) to
    prevent the 'Decimal + float' TypeError that occurs when SQLAlchemy Numeric
    columns (returning Decimal) are mixed with float values from the LLM pipeline.
    """
    exam = db.get(Exam, exam_id)

    # ── exam_max: sum of all question max scores as Decimal ──────────────────
    # Explicit for-loop accumulator: avoids sum() which can produce Decimal+float
    # TypeError when the generator yields a mix of types.
    exam_max = Decimal("0")
    for _u in question_units:
        exam_max += D(_u["max_score"])
    print(
        f"[DECIMAL-CHECK] exam_id={exam_id} exam_max={exam_max!r} "
        f"type={type(exam_max).__name__}"
    )

    # Build question_id → question metadata lookup
    q_meta: dict[str, dict] = {u["question_id"]: u for u in question_units}

    if selective:
        # Delete only the specific students being regraded
        student_ids_to_delete = [s["student_id"] for s in extractions]
        existing_result_ids = [
            r.id for r in db.query(ExamResult)
            .filter(
                ExamResult.exam_id == exam_id,
                ExamResult.student_id.in_(student_ids_to_delete),
            )
            .all()
        ]
        if existing_result_ids:
            db.query(StudentQuestionScore).filter(
                StudentQuestionScore.result_id.in_(existing_result_ids)
            ).delete(synchronize_session=False)
            db.query(ExamResult).filter(
                ExamResult.id.in_(existing_result_ids)
            ).delete(synchronize_session=False)
            db.flush()
    else:
        # Full re-grade: delete all prior results for this exam
        existing_result_ids = [
            r.id for r in db.query(ExamResult).filter(ExamResult.exam_id == exam_id).all()
        ]
        if existing_result_ids:
            db.query(StudentQuestionScore).filter(
                StudentQuestionScore.result_id.in_(existing_result_ids)
            ).delete(synchronize_session=False)
            db.query(ExamResult).filter(ExamResult.exam_id == exam_id).delete(
                synchronize_session=False
            )
            db.flush()

    graded_count    = 0
    total_score_sum = Decimal("0")

    for student in extractions:
        eid = student["extraction_id"]
        student_scores = all_final_scores.get(eid, {})

        # ── Per-student total score ───────────────────────────────────────────
        # Explicit for-loop: D() normalises every value to Decimal so float
        # scores from _round_quarter() and Decimal scores from Numeric DB
        # columns never meet the + operator without normalisation.
        total_score = Decimal("0")
        for _info in student_scores.values():
            total_score += D(_info.get("score"))

        # Safety cap: final total must never exceed the exam's maximum marks
        if exam_max > Decimal("0"):
            total_score = min(total_score, exam_max)

        # Apply no-.75 rule to the per-student total (sum of per-question scores
        # can produce a .75 total even when each question score is already clean).
        _normalized_total = D(normalize_score(total_score, exam_max))

        print(
            f"[DECIMAL-CHECK] student_id={student['student_id']} "
            f"total_score={total_score!r} normalized={_normalized_total!r} "
            f"type={type(_normalized_total).__name__}"
        )
        # Hard assertion: must never be float at this point
        assert isinstance(_normalized_total, Decimal), (
            f"[DECIMAL-ASSERT] total_score is {type(_normalized_total).__name__}, "
            f"expected Decimal — student_id={student['student_id']}"
        )
        total_score = _normalized_total

        exam_result = ExamResult(
            exam_id      = exam_id,
            student_id   = student["student_id"],
            student_name = student["student_name"],
            total_score  = round(total_score, 2),
            grade        = _letter_grade(total_score, exam_max),
            status       = GradeStatus.graded,
        )
        db.add(exam_result)
        db.flush()  # get exam_result.id
        print(f"[grading-complete] student_id={student['student_id']} total_score={round(total_score, 2)}")

        # Per-question scores
        for q_num, unit in enumerate(question_units, 1):
            qid  = unit["question_id"]
            info = student_scores.get(qid, {})
            # D() safely handles float, Decimal, int, and None
            score: Decimal = D(info.get("score"))

            # Find the student answer text for display
            item = student["answers"].get(qid)
            student_answer_text = ""
            if item:
                student_answer_text = item.student_answer or item.student_selected_option or ""

            db.add(StudentQuestionScore(
                result_id       = exam_result.id,
                question_number = q_num,
                question_text   = unit["question_text"],
                student_answer  = student_answer_text,
                ai_reasoning    = info.get("reasoning", ""),
                max_score       = unit["max_score"],
                score           = round(score, 2),
                original_score  = round(score, 2),
            ))

        graded_count    += 1
        try:
            total_score_sum += total_score   # Decimal += Decimal — guaranteed safe
        except TypeError as _te:
            print(
                f"[DECIMAL-CRASH] student_id={student['student_id']} "
                f"TypeError in total_score_sum += total_score: {_te} "
                f"total_score_sum type={type(total_score_sum).__name__} "
                f"total_score type={type(total_score).__name__}"
            )
            raise

    # ── Update exam aggregate fields ──────────────────────────────────────────
    # In selective mode, recount from all exam_results so we include
    # previously graded students that were not part of this run.
    if exam:
        if selective:
            all_results = db.query(ExamResult).filter(ExamResult.exam_id == exam_id).all()
            all_count   = len(all_results)
            # r.total_score is Numeric(6,2) → Decimal from DB, but D() guards
            # any edge case where a freshly-flushed row might still be float.
            all_total = Decimal("0")
            for _r in all_results:
                all_total += D(_r.total_score)
            print(
                f"[DECIMAL-CHECK] selective avg: all_total={all_total!r} "
                f"type={type(all_total).__name__} all_count={all_count}"
            )
            exam.graded_count = all_count
            _raw_avg = (all_total / all_count) if all_count else Decimal("0")
            exam.avg_score    = D(normalize_score(_raw_avg, float(exam_max))) if all_count else Decimal("0")
        else:
            print(
                f"[DECIMAL-CHECK] full avg: total_score_sum={total_score_sum!r} "
                f"type={type(total_score_sum).__name__} graded_count={graded_count}"
            )
            exam.graded_count = graded_count
            _raw_avg = (total_score_sum / graded_count) if graded_count else Decimal("0")
            exam.avg_score    = D(normalize_score(_raw_avg, float(exam_max))) if graded_count else Decimal("0")
        exam.status        = ExamStatus.reviewing
        exam.workflow_step = max(exam.workflow_step, 5)


# ══════════════════════════════════════════════════════════════════════════════
#  Repair / finalize: rebuild exam_results from existing grading_final_scores
# ══════════════════════════════════════════════════════════════════════════════

def finalize_job_results(job_id: int, exam_id: int, db) -> None:
    """
    Rebuild ExamResult + StudentQuestionScore rows from already-written
    GradingFinalScore rows for *job_id*, without calling any LLM.

    Use this to recover from a crash that happened after all question scores
    were stored but before (or during) the aggregation step.

    Selective mode is honoured: if the job was started with a subset of
    students (GradingJob.student_ids), only those students are (re)written;
    existing results for other students are left untouched.

    Raises ValueError if the job or its GradingFinalScore rows are not found.
    """
    import json as _json

    job = db.get(GradingJob, job_id)
    if job is None:
        raise ValueError(f"GradingJob {job_id} not found")

    # Determine which students were part of this job (None → all students)
    raw_student_ids = job.student_ids
    student_ids_for_job: Optional[list[int]] = None
    if raw_student_ids:
        try:
            parsed = _json.loads(raw_student_ids)
            if isinstance(parsed, list) and parsed:
                student_ids_for_job = [int(x) for x in parsed]
        except (ValueError, TypeError):
            pass

    # Load all GradingFinalScore rows for this job
    final_score_rows = (
        db.query(GradingFinalScore)
        .filter(GradingFinalScore.grading_job_id == job_id)
        .all()
    )
    if not final_score_rows:
        raise ValueError(
            f"No GradingFinalScore rows found for job_id={job_id}. "
            f"Cannot finalize without stored scores."
        )

    # Rebuild all_final_scores dict: {extraction_id: {question_id: {...}}}
    all_final_scores: dict[int, dict] = {}
    for fs in final_score_rows:
        eid = fs.extraction_id
        if eid not in all_final_scores:
            all_final_scores[eid] = {}
        all_final_scores[eid][fs.question_id] = {
            "score":     float(fs.final_score) if fs.final_score is not None else 0.0,
            "reasoning": fs.reasoning_summary or "",
            "matched":   fs.matched_points or "",
            "missing":   fs.missing_points or "",
        }

    print(
        f"[finalize] job_id={job_id} exam_id={exam_id} "
        f"loaded final_score_rows={len(final_score_rows)} "
        f"extractions_with_scores={len(all_final_scores)}"
    )

    # Load question units (needed for max_score and question_text)
    q_rows   = _load_questions(db, exam_id)
    all_units: list[dict] = []
    for row in q_rows:
        all_units.extend(_expand_question_units(row))

    if not all_units:
        raise ValueError(f"No question units found for exam_id={exam_id}")

    # Load all extractions; filter to job scope if selective
    extractions = _load_extractions(db, exam_id)
    if student_ids_for_job:
        sid_set     = set(student_ids_for_job)
        extractions = [e for e in extractions if e["extraction_id"] in sid_set]
        selective   = True
        print(
            f"[finalize] selective mode: student_ids={student_ids_for_job} "
            f"matched_extractions={len(extractions)}"
        )
    else:
        selective = False
        print(f"[finalize] full mode: all {len(extractions)} extractions")

    if not extractions:
        raise ValueError(
            f"No extractions match the job's student_ids for exam_id={exam_id}"
        )

    # Rebuild exam_results + student_question_scores
    _create_exam_results(db, exam_id, extractions, all_units, all_final_scores, selective)
    db.flush()

    # Find or create a report for this job; activate it after results are written
    rpt = db.query(GradingReport).filter(
        GradingReport.grading_job_id == job_id
    ).first()
    if not rpt:
        _exam_fin = db.get(Exam, exam_id)
        _fin_avg  = float(_exam_fin.avg_score) if _exam_fin and _exam_fin.avg_score is not None else None
        rpt = _create_grading_report(
            db, exam_id, job_id,
            student_ids   = student_ids_for_job,
            student_count = len(extractions),
            avg_score     = _fin_avg,
        )
    else:
        # Update avg now that exam.avg_score has been recalculated
        _exam_fin = db.get(Exam, exam_id)
        if _exam_fin and _exam_fin.avg_score is not None:
            rpt.average_score = float(_exam_fin.avg_score)

    # Activate the report (mark any existing active as replaced)
    if not rpt.is_active:
        for _prev in db.query(GradingReport).filter(
            GradingReport.exam_id   == exam_id,
            GradingReport.is_active == True,
        ).all():
            _prev.is_active = False
            _prev.status    = "replaced"
        rpt.is_active   = True
        rpt.status      = "active"
        rpt.approved_at = datetime.utcnow()

    # Mark job as completed (it previously crashed before reaching this step)
    job.status       = GradingJobStatus.completed
    job.progress     = 100
    job.completed_at = datetime.utcnow()
    job.error_message = None   # clear stale error from the crash

    db.commit()
    print(f"[finalize] job_id={job_id} marked completed, exam_results rebuilt, report activated")


# ══════════════════════════════════════════════════════════════════════════════
#  Regrading confirmation helpers
# ══════════════════════════════════════════════════════════════════════════════

def _build_all_final_scores_from_db(job_id: int, db) -> "dict[int, dict]":
    """Rebuild {extraction_id: {question_id: info}} from GradingFinalScore rows."""
    rows = (
        db.query(GradingFinalScore)
        .filter(GradingFinalScore.grading_job_id == job_id)
        .all()
    )
    if not rows:
        raise ValueError(f"No GradingFinalScore rows found for job_id={job_id}")
    result: dict = {}
    for fs in rows:
        eid = fs.extraction_id
        if eid not in result:
            result[eid] = {}
        result[eid][fs.question_id] = {
            "score":     float(fs.final_score) if fs.final_score is not None else 0.0,
            "reasoning": fs.reasoning_summary or "",
            "matched":   fs.matched_points or "",
            "missing":   fs.missing_points or "",
        }
    return result


def _resolve_student_ids_from_job(job) -> "list[int] | None":
    """Parse job.student_ids JSON → list[int] or None."""
    import json as _json
    if not job.student_ids:
        return None
    try:
        parsed = _json.loads(job.student_ids)
        if isinstance(parsed, list) and parsed:
            return [int(x) for x in parsed]
    except (ValueError, TypeError):
        pass
    return None


def apply_job_results(job_id: int, db) -> None:
    """
    Apply the stored GradingFinalScore results to ExamResult + StudentQuestionScore.
    Called when the instructor confirms "Apply new results" for a job that was
    paused at awaiting_confirmation.

    Also activates the draft GradingReport created for this job, marking any
    previously active report as "replaced".
    """
    job = db.get(GradingJob, job_id)
    if job is None:
        raise ValueError(f"GradingJob {job_id} not found")
    if job.status != GradingJobStatus.awaiting_confirmation:
        raise ValueError(
            f"Job {job_id} is not awaiting confirmation "
            f"(current status: {job.status})"
        )

    student_ids_for_job = _resolve_student_ids_from_job(job)

    all_final_scores = _build_all_final_scores_from_db(job_id, db)

    q_rows    = _load_questions(db, job.exam_id)
    all_units: list[dict] = []
    for row in q_rows:
        all_units.extend(_expand_question_units(row))
    if not all_units:
        raise ValueError(f"No question units found for exam_id={job.exam_id}")

    extractions = _load_extractions(db, job.exam_id)
    if student_ids_for_job:
        sid_set     = set(student_ids_for_job)
        extractions = [e for e in extractions if e["extraction_id"] in sid_set]
        selective   = True
    else:
        selective = False

    if not extractions:
        raise ValueError(
            f"No extractions match job {job_id}'s student_ids for exam {job.exam_id}"
        )

    selected_count = len(extractions)
    print(f"[REGRADING-APPLY] selected_count={selected_count} job_id={job_id}")

    # Write exam_results + student_question_scores; also recalculates exam.avg_score
    _create_exam_results(db, job.exam_id, extractions, all_units, all_final_scores, selective)
    db.flush()

    # Find or create the report for this job; activate it.
    _exam_applied = db.get(Exam, job.exam_id)
    _applied_avg  = float(_exam_applied.avg_score) if _exam_applied and _exam_applied.avg_score is not None else None

    draft_rpt = db.query(GradingReport).filter(
        GradingReport.grading_job_id == job_id
    ).first()
    if not draft_rpt:
        # Report was not yet created (e.g. job crashed mid-run before step 6c).
        # Build it now from GradingFinalScore data before activating.
        _batch_avg = _compute_avg_from_final_scores(job_id, extractions, db)
        draft_rpt = _create_grading_report(
            db, job.exam_id, job_id,
            student_ids   = student_ids_for_job,
            student_count = selected_count,
            avg_score     = _batch_avg,
            force_draft   = True,  # will be activated immediately below
        )
        print(f"[apply_job_results] created missing report id={draft_rpt.id} for job_id={job_id}")

    # Update avg_score now that exam.avg_score reflects all students
    if _applied_avg is not None:
        draft_rpt.average_score = _applied_avg

    # Activate this report; mark previous active ones as replaced
    for prev in db.query(GradingReport).filter(
        GradingReport.exam_id   == job.exam_id,
        GradingReport.is_active == True,
    ).all():
        prev.is_active = False
        prev.status    = "replaced"
    draft_rpt.is_active   = True
    draft_rpt.status      = "active"
    draft_rpt.approved_at = datetime.utcnow()

    job.status        = GradingJobStatus.completed
    job.progress      = 100
    job.completed_at  = datetime.utcnow()
    job.error_message = None
    db.commit()
    print(f"[apply_job_results] job_id={job_id} results applied, report activated")


def decline_job_results(job_id: int, db) -> None:
    """
    Mark the job as completed without applying any results to exam_results.
    The draft GradingReport remains in history but is NOT activated.
    Previous results for every selected student are left unchanged.
    """
    job = db.get(GradingJob, job_id)
    if job is None:
        raise ValueError(f"GradingJob {job_id} not found")
    if job.status != GradingJobStatus.awaiting_confirmation:
        raise ValueError(
            f"Job {job_id} is not awaiting confirmation "
            f"(current status: {job.status})"
        )

    student_ids_for_job = _resolve_student_ids_from_job(job)
    extractions = _load_extractions(db, job.exam_id)
    if student_ids_for_job:
        sid_set     = set(student_ids_for_job)
        extractions = [e for e in extractions if e["extraction_id"] in sid_set]

    print(
        f"[REGRADING-KEEP-PREVIOUS] selected_count={len(extractions)} job_id={job_id}"
    )

    job.status        = GradingJobStatus.completed
    job.progress      = 100
    job.completed_at  = datetime.utcnow()
    job.error_message = None
    db.commit()
    print(f"[decline_job_results] job_id={job_id} completed, previous results kept")


# ══════════════════════════════════════════════════════════════════════════════
#  Main background task entry point
# ══════════════════════════════════════════════════════════════════════════════

def run_grading_background(
    job_id:                       int,
    exam_id:                       int,
    selected_models:               list[str],
    grading_basis:                 str,
    consensus_enabled:             bool,
    general_grading_instructions:  Optional[str],
    student_ids:                   Optional[list[int]] = None,
) -> None:
    """
    Background task: orchestrate the full grading pipeline for one exam.

    Called from routers/grading.py via FastAPI BackgroundTasks.

    Steps:
      1. Mark job running
      2. Load questions, extractions, rubric
      3. For each question unit:
         a. MCQ/TF → deterministic
         b. LLM types → individual or consensus
      4. Store raw model results + final scores
      5. Create exam_results + student_question_scores
      6. Mark job completed
    """
    db = SessionLocal()

    def _fail(msg: str) -> None:
        print(f"[grading] job failed")
        print(f"[grading] failure reason: {str(msg)[:500]}")
        print(f"[grading-engine] _fail() called for job_id={job_id}: {msg}")
        try:
            # SQLAlchemy 2.0: a failed flush leaves the session in
            # PendingRollbackError state.  Must rollback before any commit.
            try:
                db.rollback()
            except Exception as rb_exc:
                print(f"[grading-engine] _fail() rollback error: {rb_exc}")
            job = db.get(GradingJob, job_id)
            if job:
                job.status        = GradingJobStatus.failed
                job.error_message = str(msg)[:2000]
                job.completed_at  = datetime.utcnow()
                db.commit()
                print(f"[grading-engine] job_id={job_id} marked as FAILED in DB")
        except Exception as e:
            print(f"[grading-engine] _fail() could not update job status: {e}")
            traceback.print_exc()
        finally:
            try:
                db.close()
            except Exception:
                pass

    try:
        # ── 1. Mark running ───────────────────────────────────────────────────
        job = db.get(GradingJob, job_id)
        if not job:
            return
        job.status     = GradingJobStatus.running
        job.started_at = datetime.utcnow()
        db.commit()

        print(f"[grading] job started: {job_id}")
        print(f"[grading-engine] job_id={job_id} exam_id={exam_id} started")

        # ── 2. Load data ──────────────────────────────────────────────────────
        print(f"[grading] loading exam data")
        print(f"[grading] loading questions")
        q_rows = _load_questions(db, exam_id)
        print(f"[grading] exam data loaded")
        print(f"[grading] loading answer key")
        # answer key is embedded in exam_model_answers (same rows as questions)
        print(f"[grading] answer key loaded: {len(q_rows)} question row(s)")
        print(f"[grading] loading rubric")
        rubric_text = _load_rubric_text(db, exam_id)
        print(f"[grading] rubric loaded: {'present' if rubric_text else 'none'}")
        print(f"[grading] loading extracted student answers")
        extractions = _load_extractions(db, exam_id)
        print(f"[grading] student answers loaded: {len(extractions)} student(s) total")

        # Filter to selected students when scope is not "all".
        # Save the full unfiltered list first so we can:
        #   • log which students are being skipped ([GRADING-SKIP-UNSELECTED])
        #   • snapshot their existing results before grading starts ([GRADING-PRESERVE-RESULT])
        #   • verify those results are identical after grading ([GRADING-PRESERVE-AFTER])
        _all_extractions_before_filter = extractions
        _student_ids_set               = set(student_ids) if student_ids else set()

        if student_ids:
            extractions            = [e for e in extractions if e["extraction_id"] in _student_ids_set]
            _unselected_extractions = [e for e in _all_extractions_before_filter
                                       if e["extraction_id"] not in _student_ids_set]

            print(
                f"[GRADING-SCOPE] mode=selected_students "
                f"selected_count={len(extractions)} "
                f"total_students={len(_all_extractions_before_filter)} "
                f"student_ids={student_ids}"
            )

            # One log line per student being SKIPPED (not selected for this run)
            for _ue in _unselected_extractions:
                print(
                    f"[GRADING-SKIP-UNSELECTED] extraction_id={_ue['extraction_id']} "
                    f"student_id={_ue['student_id']} reason=not_selected"
                )

            # One log line per student being PROCESSED (selected for this run)
            for _se in extractions:
                print(
                    f"[GRADING-PROCESS-STUDENT] extraction_id={_se['extraction_id']} "
                    f"student_id={_se['student_id']}"
                )

            # ── Before-grading snapshot of unselected students' existing results ──
            # Records their ExamResult.id, total_score, status, and
            # StudentQuestionScore count so we can verify nothing changed afterwards.
            _unselected_snapshot: dict[int, "dict | None"] = {}
            for _ue in _unselected_extractions:
                _existing = db.query(ExamResult).filter(
                    ExamResult.exam_id  == exam_id,
                    ExamResult.student_id == _ue["student_id"],
                ).first()
                if _existing:
                    _sqs_count = db.query(StudentQuestionScore).filter(
                        StudentQuestionScore.result_id == _existing.id
                    ).count()
                    _unselected_snapshot[_ue["extraction_id"]] = {
                        "student_id":  _ue["student_id"],
                        "result_id":   _existing.id,
                        "total_score": str(_existing.total_score),
                        "status":      str(_existing.status),
                        "sqs_count":   _sqs_count,
                    }
                    print(
                        f"[GRADING-PRESERVE-RESULT] extraction_id={_ue['extraction_id']} "
                        f"student_id={_ue['student_id']} "
                        f"existing_result_id={_existing.id} "
                        f"total_score={_existing.total_score} "
                        f"status={_existing.status} "
                        f"sqs_count={_sqs_count} — WILL BE PRESERVED (not regraded)"
                    )
                else:
                    _unselected_snapshot[_ue["extraction_id"]] = None
                    print(
                        f"[GRADING-PRESERVE-RESULT] extraction_id={_ue['extraction_id']} "
                        f"student_id={_ue['student_id']} no_existing_result"
                    )
        else:
            print(f"[GRADING-SCOPE] mode=all_students count={len(extractions)}")
            _unselected_extractions: list = []
            _unselected_snapshot:    dict = {}

        if not q_rows:
            _fail("No questions found for this exam. Add an answer key first.")
            return
        if not extractions:
            _fail("No student extractions found. Run extraction before grading.")
            return

        print(
            f"[grading-engine] {len(q_rows)} question rows, "
            f"{len(extractions)} students, rubric={'yes' if rubric_text else 'no'}"
        )
        for _student in extractions:
            print(
                f"[grading-start] exam_id={exam_id} "
                f"student_id={_student['student_id']} "
                f"selected_models={selected_models}"
            )

        # ── 3. Resolve model clients ──────────────────────────────────────────
        # consensus_enabled overrides model selection to always use Claude + OpenAI
        if consensus_enabled:
            clients = all_clients()   # [Claude, OpenAI]
        else:
            clients = []
            for m in selected_models:
                try:
                    clients.append(get_model_client(m))
                except ValueError as e:
                    print(f"[grading-engine] WARNING: {e}")
            if not clients:
                _fail("No valid models selected.")
                return

        mode_label = "consensus" if consensus_enabled else "individual"
        client_names = [c.name for c in clients]
        print(
            f"[GRADING] mode={mode_label} "
            f"models={client_names} "
            f"grading_basis={grading_basis}"
        )

        # ── 4. Expand all question units ──────────────────────────────────────
        all_units: list[dict] = []
        for row in q_rows:
            all_units.extend(_expand_question_units(row))

        total_units = len(all_units)
        units_done  = 0
        _job_start  = time.time()   # used for per-job ceiling check

        # {extraction_id: {question_id: {score, reasoning, matched, missing}}}
        all_final_scores: dict[int, dict] = {
            s["extraction_id"]: {} for s in extractions
        }

        # Track question IDs where LLM grading failed (exception was caught)
        failed_units: set[str] = set()

        # ── 5. Grade each unit ────────────────────────────────────────────────
        for unit in all_units:
            qid         = unit["question_id"]
            answer_type = unit["answer_type"]

            # ── Per-job ceiling check ─────────────────────────────────────
            _elapsed = time.time() - _job_start
            if _elapsed > _JOB_MAX_SECS:
                remaining = [u["question_id"] for u in all_units[units_done:]]
                _fail(
                    f"Grading job exceeded the maximum allowed duration of "
                    f"{_JOB_MAX_SECS // 3600}h ({_elapsed:.0f}s elapsed). "
                    f"Ungraded questions: {remaining}. "
                    f"Restart the job to grade remaining questions."
                )
                return

            print(
                f"[UNIT] question_id={qid} "
                f"answer_type={answer_type} "
                f"max_score={unit['max_score']}"
            )

            student_batch = _get_student_answers_for_unit(extractions, unit)

            print(f"[grading] grading question {qid}")
            print(f"[grading-question] question_id={qid} question_type={answer_type}")
            print(
                f"[grading-engine] unit={qid} type={answer_type} "
                f"students={len(student_batch)}"
            )

            # ── A. Deterministic (MCQ / True-False) ───────────────────────────
            if answer_type in _DETERMINISTIC_TYPES:
                results = _grade_deterministic(unit, student_batch)
                _store_model_grading_results(db, job_id, qid, results)

                for r in results:
                    eid    = r["extraction_id"]
                    dscore = _round_quarter(r["assigned_score"], r["max_score"])
                    all_final_scores[eid][qid] = {
                        "score":     dscore,
                        "reasoning": r["reason"],
                        "matched":   r["matched_points"],
                        "missing":   r["missing_points"],
                    }
                    _store_final_score(
                        db, job_id, eid, qid,
                        dscore, r["max_score"],
                        "deterministic", r["reason"],
                        r["matched_points"], r["missing_points"],
                    )

            # ── B. Consensus mode ─────────────────────────────────────────────
            elif consensus_enabled:
                print(
                    f"[LLM-GRADING] question_id={qid} "
                    f"answer_type={answer_type} model=consensus(claude+openai) phase=consensus_phase1"
                )
                try:
                    outcome = _grade_consensus(
                        unit, student_batch, rubric_text,
                        grading_basis, general_grading_instructions,
                    )
                except Exception as exc:
                    print(
                        f"[LLM-ERROR] question_id={qid} model=consensus "
                        f"error={type(exc).__name__}: {exc}"
                    )
                    traceback.print_exc()
                    # Fall back to zero scores for all students on this question.
                    # _store_final_score MUST be called so the question appears in
                    # grading_final_scores and therefore in the export (even as 0).
                    failed_units.add(qid)
                    for s in student_batch:
                        eid = s["extraction_id"]
                        error_reason = f"[GRADING ERROR] {type(exc).__name__}: {str(exc)[:200]}"
                        all_final_scores[eid][qid] = {
                            "score": 0.0, "reasoning": error_reason,
                            "matched": [], "missing": [],
                        }
                        _store_final_score(
                            db, job_id, eid, qid,
                            0.0, unit["max_score"],
                            "failed", error_reason, [], [],
                        )
                    units_done += 1
                    job.progress = int(units_done / total_units * 90)
                    db.commit()
                    continue

                _store_model_grading_results(db, job_id, qid, outcome["initial_results"])
                _store_model_grading_results(db, job_id, qid, outcome["revised_results"])

                # ── Global keyword guard — enrich Phase 1 matched_points ──────
                # Must run BEFORE _apply_recall_list_guard_consensus because that
                # guard reads initial_results[examiner_1]["matched_points"] to
                # determine how many concepts were correctly identified.
                # The raw LLM outputs are already stored above; enrichment here
                # affects only the final grading_final_scores, not the audit trail.
                _enriched_initial = _apply_recall_keyword_match_guard(
                    outcome["initial_results"], unit, student_batch
                )
                # Rebind so _apply_recall_list_guard_consensus sees updated data
                outcome = {**outcome, "initial_results": _enriched_initial}

                # Apply recall-list score-consistency guard to consensus final scores.
                _consensus_guarded = _apply_recall_list_guard_consensus(
                    outcome, unit, student_batch
                )

                for s in student_batch:
                    sid    = s["student_id"]
                    eid    = s["extraction_id"]
                    g      = _consensus_guarded.get(sid, {})
                    # Re-apply quarter-rounding after any guard correction.
                    # (Scores that were not raised pass through unchanged.)
                    fscore = _round_quarter(
                        g.get("score", outcome["final_scores"].get(sid, 0.0)),
                        unit["max_score"],
                    )
                    rep_reason = g.get("reason", "")

                    all_final_scores[eid][qid] = {
                        "score": fscore, "reasoning": rep_reason,
                        "matched": [], "missing": [],
                    }
                    _store_final_score(
                        db, job_id, eid, qid,
                        fscore, unit["max_score"],
                        "consensus", rep_reason, [], [],
                    )

            # ── C. Individual mode (one or more models) ───────────────────────
            else:
                # Run each client; final score = average across selected models
                all_model_results: list[list[dict]] = []

                def _run_one(c: GradingModelClient) -> list[dict]:
                    # Select prompt builder.
                    # DeepSeek in research mode uses the isolated imperfect prompt.
                    # All other models — and DeepSeek when the flag is off — use
                    # the standard individual prompt.  No shared state is mutated.
                    if c.name == "deepseek" and DEEPSEEK_RESEARCH_MODE:
                        _prompt_builder = build_deepseek_imperfect_prompt
                        print(
                            f"[LLM-GRADING] question_id={qid} "
                            f"answer_type={answer_type} model={c.name} "
                            f"phase=individual prompt=deepseek_research"
                        )
                    else:
                        _prompt_builder = None  # defaults to build_individual_prompt
                        print(
                            f"[LLM-GRADING] question_id={qid} "
                            f"answer_type={answer_type} model={c.name} phase=individual"
                        )
                    try:
                        return _grade_individual_with_model(
                            c, unit, student_batch, rubric_text,
                            grading_basis, general_grading_instructions,
                            prompt_builder=_prompt_builder,
                        )
                    except Exception as exc:
                        print(
                            f"[LLM-ERROR] question_id={qid} model={c.name} "
                            f"error={type(exc).__name__}: {exc}"
                        )
                        traceback.print_exc()
                        return []

                if len(clients) > 1:
                    with ThreadPoolExecutor(max_workers=len(clients)) as pool:
                        futs = [pool.submit(_run_one, c) for c in clients]
                        for fut in as_completed(futs):
                            _kw = _apply_recall_keyword_match_guard(
                                fut.result(), unit, student_batch
                            )
                            all_model_results.append(
                                _apply_recall_list_guard(_kw, unit)
                            )
                else:
                    _kw = _apply_recall_keyword_match_guard(
                        _run_one(clients[0]), unit, student_batch
                    )
                    all_model_results.append(
                        _apply_recall_list_guard(_kw, unit)
                    )

                for model_results in all_model_results:
                    _store_model_grading_results(db, job_id, qid, model_results)

                # If every model returned an empty result list, all API calls failed
                if all(len(mr) == 0 for mr in all_model_results):
                    failed_units.add(qid)
                    print(
                        f"[LLM-ERROR] question_id={qid} all models returned empty — "
                        f"API failure for every selected model"
                    )

                # Average scores across models per student.
                # Coerce every assigned_score to float before building the list
                # so sum() stays in float-land (Decimal + float crashes).
                for s in student_batch:
                    sid = s["student_id"]
                    eid = s["extraction_id"]
                    per_model_scores: list[float] = [
                        float(r["assigned_score"])
                        for model_results in all_model_results
                        for r in model_results
                        if r["student_id"] == sid
                    ]
                    raw_avg = (
                        sum(per_model_scores) / len(per_model_scores)
                        if per_model_scores else 0.0
                    )
                    final = _round_quarter(raw_avg, unit["max_score"])
                    print(
                        f"[ROUNDING] mode=individual "
                        f"question_id={qid} student_id={sid} "
                        f"raw={raw_avg:.4f} → final={final}"
                    )

                    # Reasoning from first successful model
                    rep_reason = next(
                        (r.get("reason", "")
                         for model_results in all_model_results
                         for r in model_results
                         if r["student_id"] == sid),
                        "",
                    )
                    rep_matched = next(
                        (r.get("matched_points", [])
                         for model_results in all_model_results
                         for r in model_results
                         if r["student_id"] == sid),
                        [],
                    )
                    rep_missing = next(
                        (r.get("missing_points", [])
                         for model_results in all_model_results
                         for r in model_results
                         if r["student_id"] == sid),
                        [],
                    )

                    # Use mode="failed" when the score is forced to 0 due to API error
                    score_mode = "failed" if qid in failed_units else "individual"
                    all_final_scores[eid][qid] = {
                        "score": final, "reasoning": rep_reason,
                        "matched": rep_matched, "missing": rep_missing,
                    }
                    _store_final_score(
                        db, job_id, eid, qid,
                        final, unit["max_score"],
                        score_mode, rep_reason,
                        rep_matched, rep_missing,
                    )

            print(f"[grading] saving grading_final_scores for question {qid}")
            db.commit()
            print(f"[grading] grading_final_scores saved for question {qid}")
            units_done += 1
            job.progress = int(units_done / total_units * 90)
            db.commit()
            print(f"[grading-engine] unit={qid} done ({units_done}/{total_units})")

        # ── 6. Completion validation ──────────────────────────────────────────
        # Verify every expected unit has a final score for every student.
        # If ALL LLM questions failed → hard-fail the job (no report, no results).
        # If SOME LLM questions failed → proceed with a warning in error_message.
        llm_units = [u for u in all_units if u["answer_type"] not in _DETERMINISTIC_TYPES]
        if llm_units and failed_units:
            n_llm    = len(llm_units)
            n_failed = len(failed_units)
            failed_ids = ", ".join(sorted(failed_units))
            if n_failed == n_llm:
                # ── Total LLM failure: do not create a report or apply results ──
                # The job is marked failed so the UI shows the real reason rather
                # than displaying a report that contains only MCQ/TF scores.
                _fail(
                    f"GRADING FAILED: All {n_llm} LLM-graded question(s) failed — "
                    f"no text-question scores were produced. "
                    f"Only MCQ/True-False questions would have been scored, which is "
                    f"not a valid grading result. "
                    f"Check the API key, model name, provider quota, and whether the "
                    f"backend was restarted after changing .env. "
                    f"Failed questions: {failed_ids}"
                )
                return
            else:
                completion_warning = (
                    f"GRADING PARTIAL: {n_failed} of {n_llm} LLM-graded question(s) "
                    f"could not be graded and received 0 marks. "
                    f"Failed questions: {failed_ids}"
                )
                print(f"[grading-engine] WARNING: {completion_warning}")
                job.error_message = completion_warning
                db.commit()

        # Catch any student×unit combinations still missing from all_final_scores
        # (should not happen after the above, but as a hard safety net)
        missing_entries: list[str] = []
        for unit in all_units:
            uqid = unit["question_id"]
            for student in extractions:
                eid = student["extraction_id"]
                if uqid not in all_final_scores.get(eid, {}):
                    missing_entries.append(f"{uqid}:student={student['student_id']}")
                    all_final_scores[eid][uqid] = {
                        "score": 0.0, "reasoning": "Score missing — grading error",
                        "matched": [], "missing": [],
                    }
                    _store_final_score(
                        db, job_id, eid, uqid,
                        0.0, unit["max_score"],
                        "missing", "Score missing — grading error", [], [],
                    )
        if missing_entries:
            print(
                f"[grading-engine] SAFETY-NET: filled {len(missing_entries)} "
                f"missing score entries: {missing_entries[:10]}"
            )
            db.commit()

        # ── 6c. Regrading confirmation check + report creation ───────────────
        # Report is ALWAYS created here (with avg computed from GradingFinalScore).
        # Grade adoption (writing to exam_results) is a separate step that only
        # happens immediately when there are no conflicts; otherwise the instructor
        # must confirm via POST /grading/confirm/{job_id}.

        _selected_student_ids = [s["student_id"] for s in extractions]
        _already_graded: int = 0
        if _selected_student_ids:
            _already_graded = db.query(ExamResult).filter(
                ExamResult.exam_id    == exam_id,
                ExamResult.student_id.in_(_selected_student_ids),
            ).count()

        # Compute avg from GradingFinalScore rows (exam.avg_score not yet updated)
        _batch_avg = _compute_avg_from_final_scores(job_id, extractions, db)

        # Always create the report: draft when confirmation is needed, auto-active otherwise
        _rpt = _create_grading_report(
            db, exam_id, job_id,
            student_ids   = student_ids,
            student_count = len(extractions),
            avg_score     = _batch_avg,
            force_draft   = (_already_graded > 0),
        )

        if _already_graded > 0:
            # Existing results detected → pause and wait for instructor confirmation.
            # The draft report is already in the DB and visible/downloadable.
            import json as _json
            print(
                f"[REGRADING-CONFIRMATION-NEEDED] "
                f"already_graded_count={_already_graded} "
                f"selected_count={len(extractions)} "
                f"report_id={_rpt.id} report_version={_rpt.version_number}"
            )
            job.status        = GradingJobStatus.awaiting_confirmation
            job.progress      = 95
            job.error_message = _json.dumps({
                "already_graded_count": _already_graded,
                "selected_count":       len(extractions),
                "report_id":            _rpt.id,
            })
            db.commit()
            print(
                f"[grading-engine] job_id={job_id} paused at awaiting_confirmation — "
                f"{_already_graded} student(s) have existing results"
            )
            return   # stop here; instructor must confirm via POST /grading/confirm/{job_id}

        # ── 7. Create exam_results + student_question_scores ──────────────────
        # Only reached when _already_graded == 0 (no confirmation needed).
        print(f"[grading] creating exam_results")
        print(f"[grading] saving student_question_scores")
        print(f"[grading-engine] writing final results to exam_results table")
        _create_exam_results(
            db, exam_id, extractions, all_units, all_final_scores,
            selective=(bool(student_ids)),
        )
        print(f"[grading] student_question_scores saved")
        print(f"[grading] exam_results created")

        # Update the report's avg_score now that exam.avg_score has been recalculated
        # by _create_exam_results (in selective mode it reflects ALL students, not
        # just this batch — so this is more accurate than _batch_avg).
        _exam_after = db.get(Exam, exam_id)
        if _exam_after and _exam_after.avg_score is not None:
            _rpt.average_score = float(_exam_after.avg_score)

        # If the report was created as draft (an active report already existed),
        # activate it now and mark the previous one as replaced.
        if not _rpt.is_active:
            for _prev in db.query(GradingReport).filter(
                GradingReport.exam_id   == exam_id,
                GradingReport.is_active == True,
            ).all():
                _prev.is_active = False
                _prev.status    = "replaced"
            _rpt.is_active   = True
            _rpt.status      = "active"
            _rpt.approved_at = datetime.utcnow()

        print(f"[grading] committing transaction")
        db.commit()
        print(f"[grading] commit successful")

        # ── After-grading verification: confirm unselected students are intact ──
        # Runs after db.commit() so we read the final committed state.
        # Any change (deletion, score mutation, added rows) is flagged as CHANGED.
        if student_ids and _unselected_extractions:
            print(f"[grading-engine] verifying {len(_unselected_extractions)} unselected student(s) are untouched...")
            for _ue in _unselected_extractions:
                _snap  = _unselected_snapshot.get(_ue["extraction_id"])
                _after = db.query(ExamResult).filter(
                    ExamResult.exam_id   == exam_id,
                    ExamResult.student_id == _ue["student_id"],
                ).first()

                if _snap is None:
                    # Had no result before — should still have none
                    if _after:
                        print(
                            f"[GRADING-PRESERVE-AFTER] UNEXPECTED: "
                            f"extraction_id={_ue['extraction_id']} "
                            f"student_id={_ue['student_id']} "
                            f"had NO result before grading but now has result_id={_after.id}"
                        )
                    else:
                        print(
                            f"[GRADING-PRESERVE-AFTER] OK: "
                            f"extraction_id={_ue['extraction_id']} "
                            f"student_id={_ue['student_id']} still has no result (UNCHANGED)"
                        )
                elif not _after:
                    print(
                        f"[GRADING-PRESERVE-AFTER] CORRUPTED: "
                        f"extraction_id={_ue['extraction_id']} "
                        f"student_id={_ue['student_id']} "
                        f"ExamResult was DELETED — was result_id={_snap['result_id']}"
                    )
                else:
                    _after_sqs = db.query(StudentQuestionScore).filter(
                        StudentQuestionScore.result_id == _after.id
                    ).count()
                    _id_changed    = _after.id               != _snap["result_id"]
                    _score_changed = str(_after.total_score) != _snap["total_score"]
                    _stat_changed  = str(_after.status)      != _snap["status"]
                    _sqs_changed   = _after_sqs              != _snap["sqs_count"]
                    if _id_changed or _score_changed or _stat_changed or _sqs_changed:
                        print(
                            f"[GRADING-PRESERVE-AFTER] CHANGED: "
                            f"extraction_id={_ue['extraction_id']} "
                            f"student_id={_ue['student_id']} "
                            f"result_id: {_snap['result_id']}->{_after.id} "
                            f"total_score: {_snap['total_score']}->{_after.total_score} "
                            f"status: {_snap['status']}->{_after.status} "
                            f"sqs_count: {_snap['sqs_count']}->{_after_sqs}"
                        )
                    else:
                        print(
                            f"[GRADING-PRESERVE-AFTER] OK: "
                            f"extraction_id={_ue['extraction_id']} "
                            f"student_id={_ue['student_id']} "
                            f"result_id={_after.id} "
                            f"total_score={_after.total_score} "
                            f"sqs_count={_after_sqs} (UNCHANGED)"
                        )

        # ── 8. Mark job complete ──────────────────────────────────────────────
        # Report was already created in step 6c; just complete the job.
        job.status       = GradingJobStatus.completed
        job.progress     = 100
        job.completed_at = datetime.utcnow()

        print(f"[grading] committing transaction")
        db.commit()
        print(f"[grading] commit successful")

        print(f"[grading] job completed successfully")
        print(f"[grading-engine] job_id={job_id} completed successfully")

        # ── 9. Auto-trigger gap analysis ──────────────────────────────────────
        # Runs in the same background thread as grading, after results are
        # committed.  Uses a fresh session so any failure cannot corrupt the
        # grading session.  Errors are caught inside trigger_gap_analysis().
        try:
            from services.gap_analysis_engine import trigger_gap_analysis
            trigger_gap_analysis(exam_id)   # opens its own SessionLocal
        except Exception as gap_exc:
            print(f"[grading-engine] gap analysis auto-trigger error: {gap_exc}")

    except Exception as exc:
        print(f"[grading-engine] FATAL ERROR job_id={job_id}: {exc}")
        traceback.print_exc()
        _fail(str(exc))
