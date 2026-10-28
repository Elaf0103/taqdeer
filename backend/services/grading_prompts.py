"""
Grading Prompt Builders
=======================
These functions are the only permitted way to create prompts for the grading
pipeline.  Every prompt is rendered by filling the canonical templates defined
in services.prompt_templates using the fill_template() renderer.

No prompt string is assembled by hand in this file or anywhere else in the
backend.  The raw template strings live in prompt_templates.py; this file
provides typed, documented builder functions that validate inputs and call
fill_template() with the correct keyword arguments.

Three builder functions
-----------------------
  build_individual_prompt        — independent grading by one model
  build_consensus_phase1_prompt  — consensus round 1: each examiner grades alone
  build_consensus_phase2_prompt  — consensus round 2: examiners review each other

MCQ / True-False
----------------
These builder functions are NEVER called for MCQ or True/False questions.
Those are graded deterministically in grading_engine.py without involving
any prompt or LLM.
"""

from __future__ import annotations

from typing import Optional

from services.prompt_templates import (
    consensus_discussion_prompt_template,
    deepseek_imperfect_grading_prompt,
    fill_template,
    individual_grading_prompt_template,
)


# ══════════════════════════════════════════════════════════════════════════════
#  Student answers batch formatter  (shared by all builders)
# ══════════════════════════════════════════════════════════════════════════════

def _format_student_batch(student_answers: list[dict]) -> str:
    """
    Render a list of student-answer dicts into the numbered text block that
    both grading templates expect as {{STUDENT_ANSWERS_BATCH}}.

    Each dict must contain:
        student_id   str  — identifier stored in the DB
        student_name str  — display name
        answer       str  — the student's written answer (may be empty)

    Optional keys:
        selected_option     str | None  — MCQ selected letter
        drawing_description str | None  — text description of a student diagram
        has_drawing         bool        — True when a diagram image is attached
    """
    lines: list[str] = []
    for idx, s in enumerate(student_answers, 1):
        sid  = s.get("student_id",   f"S{idx}")
        name = s.get("student_name", "Unknown")
        ans  = (s.get("answer") or "").strip() or "[NO ANSWER PROVIDED]"
        opt  = s.get("selected_option")

        lines.append(f"[{idx}] student_id={sid} | name={name}")
        if opt:
            lines.append(f"     Selected option: {opt}")
        lines.append(f"     Answer: {ans}")

        # Diagram / drawing context — critical for diagrammatic questions
        if s.get("drawing_description"):
            lines.append(f"     Drawing description: {s['drawing_description']}")
        if s.get("has_drawing"):
            lines.append(
                "     [Student diagram attached as image — "
                "evaluate the visual content alongside the text answer above]"
            )

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
#  Examiner comparison block formatter  (Phase 2 only)
# ══════════════════════════════════════════════════════════════════════════════

def _format_examiner_comparison(
    examiner_label:    str,
    student_answers:   list[dict],
    all_initial_scores: dict[str, dict],
) -> str:
    """
    Render the per-student comparison block for consensus Phase 2.

    Used as {{OTHER_EXAMINERS_ASSESSMENTS}} in the consensus template.

    Parameters
    ----------
    examiner_label : str
        The label of the examiner receiving this prompt ("Examiner 1" etc.).
    student_answers : list[dict]
        Same student list passed to the Phase 1 prompt.
    all_initial_scores : dict
        Structure: {student_id: {examiner_label: {"score": X, "reason": "…"}}}

    Returns
    -------
    str
        Formatted multi-line block showing every student's initial scores
        from all three examiners, clearly labelling which entry is the
        receiving examiner's own assessment.
    """
    lines: list[str] = []
    for idx, s in enumerate(student_answers, 1):
        sid     = s.get("student_id", f"S{idx}")
        name    = s.get("student_name", "Unknown")
        ans     = (s.get("answer") or "").strip() or "[NO ANSWER PROVIDED]"
        scores  = all_initial_scores.get(sid, {})

        lines.append(f"[{idx}] student_id={sid} | name={name}")
        lines.append(f"     Answer: {ans}")

        for label, entry in scores.items():
            score  = entry.get("score",  "N/A")
            reason = entry.get("reason", "")
            marker = "  ◄ YOUR SCORE" if label == examiner_label else ""
            lines.append(f"     {label}: {score} marks — {reason}{marker}")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
#  1. Individual Grading Prompt
# ══════════════════════════════════════════════════════════════════════════════

def build_individual_prompt(
    question_id:                   str,
    question_text:                 str,
    max_score:                     float,
    grading_basis:                 str,
    answer_key_text:               str,
    student_answers:               list[dict],
    answer_type:                   str = "recall_general",
    rubric_text:                   Optional[str] = None,
    general_grading_instructions:  Optional[str] = None,
    scoring_method:                Optional[str] = None,
    additional_checks:             Optional[str] = None,
    custom_instructions:           Optional[str] = None,
    diagram_context:               Optional[str] = None,
) -> str:
    """
    Render the individual grading prompt for one question across all students.

    The prompt instructs one model to grade every student in student_answers
    for question question_id.

    Parameters
    ----------
    question_id : str
        The question identifier, e.g. "Q1a" or "Q2".
    question_text : str
        Full text of the question.
    max_score : float
        Maximum marks available for this question.
    grading_basis : str
        "rubric" | "answer_key" | "both"
    answer_key_text : str
        The correct answer or model answer.
    student_answers : list[dict]
        Dicts with keys: student_id, student_name, answer.
    rubric_text : str | None
        Rubric content. Omitted from the prompt when None.
    general_grading_instructions : str | None
        Page-level instructor notes. Omitted when None.
    scoring_method : str | None
        Question-level scoring rule. Omitted when None.
    additional_checks : str | None
        Extra deduction / bonus rules. Omitted when None.
    custom_instructions : str | None
        Highest-priority per-question override. Omitted when None.

    Returns
    -------
    str
        Fully rendered prompt, ready to send to any grading model.
    """
    return fill_template(
        individual_grading_prompt_template,
        QUESTION_ID                    = question_id,
        QUESTION_TEXT                  = question_text,
        ANSWER_TYPE                    = answer_type,
        MAX_SCORE                      = max_score,
        GRADING_BASIS                  = grading_basis,
        ANSWER_KEY_TEXT                = answer_key_text,
        STUDENT_ANSWERS_BATCH          = _format_student_batch(student_answers),
        RUBRIC_TEXT                    = rubric_text,
        GENERAL_GRADING_INSTRUCTIONS   = general_grading_instructions,
        SCORING_METHOD                 = scoring_method,
        ADDITIONAL_CHECKS              = additional_checks,
        QUESTION_CUSTOM_INSTRUCTIONS   = custom_instructions,
        DIAGRAM_CONTEXT                = diagram_context,
    )


# ══════════════════════════════════════════════════════════════════════════════
#  1b. DeepSeek Isolated Research Prompt
# ══════════════════════════════════════════════════════════════════════════════
#
#  ISOLATION CONTRACT
#  ------------------
#  • Drop-in replacement for build_individual_prompt().
#  • Identical signature and return type — same kwargs, same str output.
#  • Uses deepseek_imperfect_grading_prompt template instead of the standard
#    individual_grading_prompt_template.
#  • NEVER call this function for any model other than "deepseek".
#  • NEVER call this function when DEEPSEEK_RESEARCH_MODE is False.
#  • NEVER used in consensus mode — DeepSeek is excluded from consensus.
#  • Does NOT touch, import, or modify individual_grading_prompt_template.
#  • Does NOT affect scoring normalization, guards, or any shared logic.
#
# ══════════════════════════════════════════════════════════════════════════════

def build_deepseek_imperfect_prompt(
    question_id:                   str,
    question_text:                 str,
    max_score:                     float,
    grading_basis:                 str,
    answer_key_text:               str,
    student_answers:               list[dict],
    answer_type:                   str = "recall_general",
    rubric_text:                   Optional[str] = None,
    general_grading_instructions:  Optional[str] = None,
    scoring_method:                Optional[str] = None,
    additional_checks:             Optional[str] = None,
    custom_instructions:           Optional[str] = None,
    diagram_context:               Optional[str] = None,
) -> str:
    """
    Render the DeepSeek isolated research prompt for one question.

    This is a drop-in replacement for build_individual_prompt() with an
    identical signature.  It uses deepseek_imperfect_grading_prompt, which
    instructs DeepSeek to produce deliberately imperfect, human-like grading.

    ISOLATION GUARANTEES
    --------------------
    • This function is called ONLY when model_key == "deepseek" AND
      DEEPSEEK_RESEARCH_MODE is True (enforced by grading_engine.py).
    • It NEVER modifies individual_grading_prompt_template or any shared state.
    • The output JSON shape is IDENTICAL to build_individual_prompt() so the
      grading engine parses it without any special-casing.
    • DeepSeek is already excluded from consensus mode via all_clients();
      this function is therefore never reachable from the consensus path.
    """
    return fill_template(
        deepseek_imperfect_grading_prompt,
        QUESTION_ID                    = question_id,
        QUESTION_TEXT                  = question_text,
        ANSWER_TYPE                    = answer_type,
        MAX_SCORE                      = max_score,
        GRADING_BASIS                  = grading_basis,
        ANSWER_KEY_TEXT                = answer_key_text,
        STUDENT_ANSWERS_BATCH          = _format_student_batch(student_answers),
        RUBRIC_TEXT                    = rubric_text,
        GENERAL_GRADING_INSTRUCTIONS   = general_grading_instructions,
        SCORING_METHOD                 = scoring_method,
        ADDITIONAL_CHECKS              = additional_checks,
        QUESTION_CUSTOM_INSTRUCTIONS   = custom_instructions,
        DIAGRAM_CONTEXT                = diagram_context,
    )


# ══════════════════════════════════════════════════════════════════════════════
#  2. Consensus Phase 1 Prompt
# ══════════════════════════════════════════════════════════════════════════════

def build_consensus_phase1_prompt(
    examiner_label:                str,
    question_id:                   str,
    question_text:                 str,
    max_score:                     float,
    grading_basis:                 str,
    answer_key_text:               str,
    student_answers:               list[dict],
    answer_type:                   str = "recall_general",
    rubric_text:                   Optional[str] = None,
    general_grading_instructions:  Optional[str] = None,
    scoring_method:                Optional[str] = None,
    additional_checks:             Optional[str] = None,
    custom_instructions:           Optional[str] = None,
    diagram_context:               Optional[str] = None,
) -> str:
    """
    Render the consensus Phase 1 prompt for one examiner.

    Phase 1 is structurally identical to individual grading — the model grades
    independently.  The only difference is that the model is addressed by its
    examiner label so it understands its role in the consensus process.

    Parameters
    ----------
    examiner_label : str
        "Examiner 1" | "Examiner 2" | "Examiner 3"

    All other parameters are the same as build_individual_prompt().

    Returns
    -------
    str
        Fully rendered Phase 1 prompt.
    """
    # Phase 1 uses the individual template — the examiner identity is prepended
    # as a brief header so the model knows it is part of a consensus panel.
    header = (
        f"You are {examiner_label}, one of two independent examiners.\n"
        "Grade independently. Do NOT adjust your score to match what other\n"
        "examiners might think. Your individual assessment will be compared\n"
        "with theirs in a subsequent discussion round.\n"
        "─" * 60
    )

    body = fill_template(
        individual_grading_prompt_template,
        QUESTION_ID                    = question_id,
        QUESTION_TEXT                  = question_text,
        ANSWER_TYPE                    = answer_type,
        MAX_SCORE                      = max_score,
        GRADING_BASIS                  = grading_basis,
        ANSWER_KEY_TEXT                = answer_key_text,
        STUDENT_ANSWERS_BATCH          = _format_student_batch(student_answers),
        RUBRIC_TEXT                    = rubric_text,
        GENERAL_GRADING_INSTRUCTIONS   = general_grading_instructions,
        SCORING_METHOD                 = scoring_method,
        ADDITIONAL_CHECKS              = additional_checks,
        QUESTION_CUSTOM_INSTRUCTIONS   = custom_instructions,
        DIAGRAM_CONTEXT                = diagram_context,
    )

    return f"{header}\n\n{body}"


# ══════════════════════════════════════════════════════════════════════════════
#  3. Consensus Phase 2 Prompt  (discussion / revision round)
# ══════════════════════════════════════════════════════════════════════════════

def build_consensus_phase2_prompt(
    examiner_label:                str,
    question_id:                   str,
    question_text:                 str,
    max_score:                     float,
    grading_basis:                 str,
    answer_key_text:               str,
    student_answers:               list[dict],
    all_initial_scores:            dict[str, dict],
    answer_type:                   str = "recall_general",
    rubric_text:                   Optional[str] = None,
    general_grading_instructions:  Optional[str] = None,
    scoring_method:                Optional[str] = None,
    additional_checks:             Optional[str] = None,
    custom_instructions:           Optional[str] = None,
    diagram_context:               Optional[str] = None,
) -> str:
    """
    Render the consensus Phase 2 (discussion) prompt for one examiner.

    Each examiner receives all other examiners' Phase 1 scores and reasons,
    then decides whether to keep or revise their own score.

    Parameters
    ----------
    examiner_label : str
        "Examiner 1" | "Examiner 2" | "Examiner 3"
    all_initial_scores : dict
        Structure: {student_id: {examiner_label: {"score": X, "reason": "…"}}}
        Must contain entries for every examiner including the one being prompted.

    All other parameters are the same as build_individual_prompt().

    Returns
    -------
    str
        Fully rendered Phase 2 consensus discussion prompt.
    """
    comparison_block = _format_examiner_comparison(
        examiner_label     = examiner_label,
        student_answers    = student_answers,
        all_initial_scores = all_initial_scores,
    )

    return fill_template(
        consensus_discussion_prompt_template,
        EXAMINER_LABEL                 = examiner_label,
        QUESTION_ID                    = question_id,
        QUESTION_TEXT                  = question_text,
        ANSWER_TYPE                    = answer_type,
        MAX_SCORE                      = max_score,
        GRADING_BASIS                  = grading_basis,
        ANSWER_KEY_TEXT                = answer_key_text,
        OTHER_EXAMINERS_ASSESSMENTS    = comparison_block,
        RUBRIC_TEXT                    = rubric_text,
        GENERAL_GRADING_INSTRUCTIONS   = general_grading_instructions,
        SCORING_METHOD                 = scoring_method,
        ADDITIONAL_CHECKS              = additional_checks,
        QUESTION_CUSTOM_INSTRUCTIONS   = custom_instructions,
        DIAGRAM_CONTEXT                = diagram_context,
    )
