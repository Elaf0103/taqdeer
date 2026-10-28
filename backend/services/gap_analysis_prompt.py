"""
Gap Analysis Prompt
===================
Qwen system prompt and input serialiser for the section-level learning gap analyser.

This is NOT a grading module.  It performs DIAGNOSTIC INTERPRETATION of
aggregated per-question performance statistics — it never assigns scores,
never modifies exam data, and never invents rubric content.

The provider is Qwen (via OpenRouter).  The LLM is given:
  • Exam metadata (name, type, section)
  • Per-question aggregated statistics (avg %, band distribution, mistakes)
  • Question text and answer-key summary for context

The LLM must return a structured JSON document matching the schema below.
"""

from __future__ import annotations

import json
from typing import Any


# ── Output JSON schema reference (for prompt injection) ───────────────────────

_OUTPUT_SCHEMA = """
{
  "questions": [
    {
      "question_number": <int>,
      "gap_detected": <true|false>,
      "severity": <"NONE"|"LOW"|"MEDIUM"|"HIGH"|"CRITICAL">,
      "issue_type": <"none"|"recall_weakness"|"procedural_misunderstanding"|"concept_confusion"|"calculation_error"|"terminology_confusion"|"question_misinterpretation">,
      "conceptual_explanation": "<string — explain what specific understanding is missing or wrong>",
      "intervention": "<string — one concrete instructional action to address this gap>"
    }
  ],
  "clusters": [
    {
      "cluster_name": "<short descriptive label>",
      "question_numbers": [<int>, ...],
      "shared_weakness": "<string — what conceptual thread links these questions>"
    }
  ],
  "section_summary": {
    "top_3_weaknesses": ["<string>", "<string>", "<string>"],
    "top_3_strengths": ["<string>", "<string>", "<string>"],
    "instructional_plan": "<string — 2-4 sentence recommended adjustment plan for the instructor>"
  }
}
"""

_SEVERITY_GUIDE = """
Severity guide (based on class average % for a question):
  NONE      — class avg ≥ 80 %  (strong performance, no gap)
  LOW       — class avg 65–79 % (minor weakness, monitor)
  MEDIUM    — class avg 50–64 % (notable gap, targeted reteaching recommended)
  HIGH      — class avg 35–49 % (significant gap, reteaching required)
  CRITICAL  — class avg < 35 %  (severe gap, fundamental misconception)

These are GUIDELINES.  You may adjust severity one level up or down if the
distribution data (e.g. bimodal split, near-zero tail) suggests it, but you
MUST justify the adjustment in conceptual_explanation.
"""

_ISSUE_TYPES = """
Issue type definitions:
  none                       — No gap detected (severity NONE or LOW with no concern)
  recall_weakness            — Students cannot recall a fact or definition
  procedural_misunderstanding — Students know the concept but apply steps incorrectly
  concept_confusion          — Students conflate two or more related concepts
  calculation_error          — Arithmetic/algebraic errors, not conceptual failures
  terminology_confusion      — Wrong term used for a correct underlying idea
  question_misinterpretation — Students answered a different question than was asked
"""

_SYSTEM_PROMPT = """\
You are an expert educational diagnostician assisting an instructor at a university.

Your task: perform SECTION-LEVEL LEARNING GAP ANALYSIS on the aggregated performance
statistics from a graded exam section.

CRITICAL RULES:
1. You are NOT grading.  Do not assign, suggest, or change any student scores.
2. Do not invent rubric criteria.  Only use the question text, answer-key summary,
   and performance statistics that are explicitly given to you.
3. Do not name or identify individual students.  All analysis is class-level only.
4. Output ONLY the JSON object described below.  No preamble, no explanation outside
   the JSON, no markdown fences.
5. All string values must be in English.
6. If fewer than 3 distinct strengths or weaknesses can be identified from the data,
   provide only what the data supports — do not fabricate entries.

{severity_guide}

{issue_types}

OUTPUT FORMAT — respond with exactly one JSON object matching this schema:
{output_schema}

CLUSTERS: group questions only when they share a clear conceptual thread (e.g. all
test the same algorithm, all test the same definition family).  If no meaningful
cluster exists, return an empty array.

SECTION SUMMARY: base the instructional_plan on the most impactful gaps only.
Keep it actionable and concise (2–4 sentences).
"""


def build_gap_analysis_prompt(
    exam_name: str,
    exam_type: str,
    exam_section: str,
    questions: list[dict[str, Any]],
) -> str:
    """
    Build the full prompt string to send to the LLM.

    Parameters
    ----------
    exam_name    : display name of the exam
    exam_type    : "midterm" / "final" / "quiz"
    exam_section : section identifier (e.g. "CS-A")
    questions    : list of per-question stat dicts, each containing:
        question_number          : int
        question_text            : str
        answer_key_summary       : str  (brief, no full answer)
        class_average_pct        : float (0–100)
        number_of_students       : int
        band_0_20                : int  (# students scoring 0–20 %)
        band_21_40               : int
        band_41_60               : int
        band_61_80               : int
        band_81_100              : int
        max_score                : float

    Returns
    -------
    str — the complete prompt to send to the LLM as a user message.
    """
    system = _SYSTEM_PROMPT.format(
        severity_guide=_SEVERITY_GUIDE,
        issue_types=_ISSUE_TYPES,
        output_schema=_OUTPUT_SCHEMA,
    )

    # Build the data block
    lines: list[str] = [
        f"EXAM: {exam_name}",
        f"TYPE: {exam_type}",
        f"SECTION: {exam_section}",
        f"NUMBER OF QUESTIONS: {len(questions)}",
        "",
        "PER-QUESTION PERFORMANCE DATA:",
        "=" * 60,
    ]

    for q in questions:
        n_students = q.get("number_of_students", 1) or 1
        lines += [
            f"",
            f"Question {q['question_number']}",
            f"  Text        : {q['question_text']}",
            f"  Answer key  : {q['answer_key_summary']}",
            f"  Max score   : {q['max_score']}",
            f"  Students    : {n_students}",
            f"  Class avg % : {q['class_average_pct']:.1f}%",
            f"  Score bands :",
            f"    0–20%   (Gap)    : {q['band_0_20']} students",
            f"    21–40%  (Weak)   : {q['band_21_40']} students",
            f"    41–60%  (Fair)   : {q['band_41_60']} students",
            f"    61–80%  (Good)   : {q['band_61_80']} students",
            f"    81–100% (Strong) : {q['band_81_100']} students",
        ]

    lines += [
        "",
        "=" * 60,
        "Analyse the data above and return the JSON gap analysis.",
    ]

    data_block = "\n".join(lines)
    return system + "\n\n" + data_block
