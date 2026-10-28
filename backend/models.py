import enum
from sqlalchemy import (
    Column, Integer, SmallInteger, String, Text, Date,
    Boolean, Enum, Numeric, DateTime, ForeignKey, func,
    UniqueConstraint,
)
from database import Base


# ══════════════════════════════════════════════════════════════════════════════
#  Shared Enums
# ══════════════════════════════════════════════════════════════════════════════

class ExamType(str, enum.Enum):
    midterm = "midterm"
    final   = "final"
    quiz    = "quiz"


class Department(str, enum.Enum):
    cs  = "cs"
    is_ = "is"
    it  = "it"


class ExamStatus(str, enum.Enum):
    pending   = "pending"
    reviewing = "reviewing"
    graded    = "graded"


class Semester(str, enum.Enum):
    first  = "first"
    second = "second"
    summer = "summer"


class ExtractionStatus(str, enum.Enum):
    pending   = "pending"
    extracted = "extracted"
    failed    = "failed"


# ══════════════════════════════════════════════════════════════════════════════
#  Table 1 — exams  (manage-exams.html)
# ══════════════════════════════════════════════════════════════════════════════

class Exam(Base):
    __tablename__ = "exams"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Ownership — NULL for legacy/seeded rows; set to creator's user id on new exams
    owner_id  = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"),
                       nullable=True, index=True)

    # Core fields — Add/Edit modal
    name          = Column(String(255),  nullable=False)
    type          = Column(Enum(ExamType), nullable=False)
    date          = Column(Date,           nullable=False, index=True)
    section       = Column(String(50),   nullable=False)
    department    = Column(Enum(Department), nullable=False, index=True)
    student_count = Column(SmallInteger, nullable=False)
    total_marks   = Column(SmallInteger, nullable=False)

    # Grading progress — updated by Answer Files / Auto-Grading steps
    answer_files_count = Column(SmallInteger, nullable=False, default=0)  # PDFs uploaded by instructor
    graded_count       = Column(SmallInteger, nullable=False, default=0)
    avg_score          = Column(Numeric(5, 2), nullable=False, default=0.00)

    # Semester the exam belongs to
    semester      = Column(Enum(Semester), nullable=True, index=True)

    # Status pill on each card
    status        = Column(Enum(ExamStatus), nullable=False,
                           default=ExamStatus.pending, index=True)

    # Workflow progress bar (1 = Exam Info … 5 = Review Grades)
    workflow_step = Column(SmallInteger, nullable=False, default=1)

    # Archive flag — set to True when instructor archives a completed exam
    is_archived = Column(Boolean, nullable=False, default=False, index=True)

    # Audit
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False,
                        server_default=func.now(), onupdate=func.now())


# ══════════════════════════════════════════════════════════════════════════════
#  Table 2 — rubrics  (rubrics.html — Section 1)
# ══════════════════════════════════════════════════════════════════════════════

class Rubric(Base):
    __tablename__ = "rubrics"

    id        = Column(Integer, primary_key=True, autoincrement=True)
    exam_id   = Column(Integer,
                       ForeignKey("exams.id", ondelete="CASCADE"),
                       nullable=False, index=True)

    # PDF metadata (binary stored in IndexedDB on frontend / filesystem on server)
    file_name = Column(String(255), nullable=False)   # original filename
    file_size = Column(Integer,     nullable=False)   # bytes
    file_path = Column(String(512), nullable=True)    # server-side storage path

    uploaded_at = Column(DateTime, nullable=False, server_default=func.now())


# ══════════════════════════════════════════════════════════════════════════════
#  Table 3 — exam_model_answers  (rubrics.html — Section 2, Answer Key)
# ══════════════════════════════════════════════════════════════════════════════

class ExamModelAnswer(Base):
    __tablename__ = "exam_model_answers"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    exam_id         = Column(Integer,
                             ForeignKey("exams.id", ondelete="CASCADE"),
                             nullable=False, index=True)

    question_number = Column(SmallInteger,  nullable=False)  # 1-based order
    question_text   = Column(Text,          nullable=False)  # the exam question
    model_answer    = Column(Text,          nullable=False)  # reference answer
    score           = Column(Numeric(6, 2), nullable=False)  # max marks (≤ 999.99)
    answer_type     = Column(String(50),    nullable=False, default="recall_general")  # supervisor's answer_type mapped to frontend key

    # Grading configuration — set by the instructor per question
    # (NULL until instructor configures; all optional)
    scoring_method       = Column(Text, nullable=True)   # e.g. "Full marks for correct key term only"
    additional_checks    = Column(Text, nullable=True)   # e.g. "Deduct 0.5 for each spelling error"
    custom_instructions  = Column(Text, nullable=True)   # Highest-priority override instructions

    # Diagram answer key images — JSON array of file paths written by the extraction pipeline.
    # Non-null only for diagrammatic question types (diagrammatic_aoa, diagrammatic_general, etc.).
    # Paths are relative to the backend/ working directory.
    # Example: '["uploads/answer_key/1/output/answer_key_drawings/answer_key_Q1a_page1.png"]'
    answer_key_images    = Column(Text, nullable=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False,
                        server_default=func.now(), onupdate=func.now())


# ══════════════════════════════════════════════════════════════════════════════
#  Table 4 — users  (login.html · register.html · profile.html · admin-profile)
# ══════════════════════════════════════════════════════════════════════════════

class UserRole(str, enum.Enum):
    instructor = "instructor"
    admin      = "admin"


class ProfessionalTitle(str, enum.Enum):
    dr   = "dr"
    prof = "prof"
    mr   = "mr"
    ms   = "ms"
    mrs  = "mrs"
    miss = "miss"
    eng  = "eng"
    none = "none"


class User(Base):
    __tablename__ = "users"

    id            = Column(Integer, primary_key=True, autoincrement=True)

    # Register form fields
    full_name          = Column(String(255), nullable=False)
    email              = Column(String(255), nullable=False, unique=True, index=True)
    password_hash      = Column(String(255), nullable=False)
    department         = Column(Enum(Department), nullable=True)
    professional_title = Column(Enum(ProfessionalTitle), nullable=True,
                                default=ProfessionalTitle.none)

    # Admin vs instructor distinction (admin-profile vs profile)
    role          = Column(Enum(UserRole), nullable=False,
                           default=UserRole.instructor, index=True)

    # Profile page — avatar upload (path stored server-side)
    avatar_path   = Column(String(512), nullable=True)

    # Active flag — admin can deactivate accounts
    is_active = Column(Boolean, nullable=False, default=True)

    # Audit
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False,
                        server_default=func.now(), onupdate=func.now())


# ══════════════════════════════════════════════════════════════════════════════
#  Table 5 — password_reset_tokens  (reset-password.html)
# ══════════════════════════════════════════════════════════════════════════════

class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id         = Column(Integer,  primary_key=True, autoincrement=True)
    user_id    = Column(Integer,  ForeignKey("users.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    email      = Column(String(255), nullable=False, index=True)
    code_hash  = Column(String(255), nullable=False)   # PBKDF2 hash of the 6-digit code
    expires_at = Column(DateTime, nullable=False)
    used_at    = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, server_default=func.now())


# ══════════════════════════════════════════════════════════════════════════════
#  Table 6 — answer_files  (answer-files.html)
# ══════════════════════════════════════════════════════════════════════════════

class AnswerFile(Base):
    __tablename__ = "answer_files"

    id        = Column(Integer, primary_key=True, autoincrement=True)
    exam_id   = Column(Integer,
                       ForeignKey("exams.id", ondelete="CASCADE"),
                       nullable=False, index=True)

    # Uploaded student answer PDF metadata
    file_name = Column(String(255), nullable=False)   # original filename
    file_size = Column(Integer,     nullable=False)   # bytes
    file_path = Column(String(512), nullable=True)    # server-side storage path

    uploaded_at = Column(DateTime, nullable=False, server_default=func.now())

    # Extraction tracking — updated by the background extraction task
    extraction_status = Column(Enum(ExtractionStatus), nullable=True)   # NULL = never attempted
    extraction_error  = Column(Text, nullable=True)


# ══════════════════════════════════════════════════════════════════════════════
#  Table 6 — grading_jobs  (auto-grading.html)
# ══════════════════════════════════════════════════════════════════════════════

class GradingJobStatus(str, enum.Enum):
    pending                = "pending"
    running                = "running"
    completed              = "completed"
    failed                 = "failed"
    awaiting_confirmation  = "awaiting_confirmation"   # paused: existing results detected


class GradingJob(Base):
    __tablename__ = "grading_jobs"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    exam_id      = Column(Integer,
                          ForeignKey("exams.id", ondelete="CASCADE"),
                          nullable=False, index=True)

    status       = Column(Enum(GradingJobStatus), nullable=False,
                          default=GradingJobStatus.pending, index=True)
    progress     = Column(SmallInteger, nullable=False, default=0)  # 0–100 %

    # Grading configuration stored with the job for auditability
    selected_models              = Column(String(255), nullable=True)  # JSON array: ["claude","qwen"]
    grading_basis                = Column(String(20),  nullable=True)  # "rubric"|"answer_key"|"both"
    consensus_enabled            = Column(Boolean,     nullable=True,  default=False)
    general_grading_instructions = Column(Text,        nullable=True)
    error_message                = Column(Text,        nullable=True)  # set on failure
    student_ids                  = Column(Text,        nullable=True)  # JSON array of extraction IDs; NULL = all students

    started_at   = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at   = Column(DateTime, nullable=False, server_default=func.now())


# ══════════════════════════════════════════════════════════════════════════════
#  Table 7 — exam_results  (exam-results · exam-review · results-reviews)
#
#  Stores the graded result record for each student per exam.
#  "submission" terminology is intentionally avoided: in Taqdeer, instructors
#  upload answer PDFs on behalf of students — students do not self-submit.
# ══════════════════════════════════════════════════════════════════════════════

class GradeStatus(str, enum.Enum):
    graded         = "graded"
    pending_review = "pending_review"
    approved       = "approved"
    overridden     = "overridden"   # instructor manually changed a score


class ExamResult(Base):
    __tablename__ = "exam_results"
    __table_args__ = (
        UniqueConstraint('exam_id', 'student_id', name='uq_exam_results_exam_student'),
    )

    id             = Column(Integer, primary_key=True, autoincrement=True)
    exam_id        = Column(Integer,
                            ForeignKey("exams.id", ondelete="CASCADE"),
                            nullable=False, index=True)

    # Student identity (format used in UI: "STU-001")
    student_id     = Column(String(20),  nullable=False, index=True)
    student_name   = Column(String(255), nullable=False)

    # Aggregated result
    total_score     = Column(Numeric(6, 2), nullable=False, default=0.00)
    grade           = Column(String(5),    nullable=True)   # "A", "B+", etc.
    status          = Column(Enum(GradeStatus), nullable=False,
                             default=GradeStatus.graded, index=True)
    manually_edited = Column(Boolean, nullable=False, default=False)

    # Audit
    created_at = Column(DateTime, nullable=False, server_default=func.now())
    updated_at = Column(DateTime, nullable=False,
                        server_default=func.now(), onupdate=func.now())


# ══════════════════════════════════════════════════════════════════════════════
#  Table 8 — student_question_scores  (exam-review.html)
# ══════════════════════════════════════════════════════════════════════════════

class StudentQuestionScore(Base):
    __tablename__ = "student_question_scores"

    id        = Column(Integer, primary_key=True, autoincrement=True)
    result_id = Column(Integer,
                       ForeignKey("exam_results.id", ondelete="CASCADE"),
                       nullable=False, index=True)

    question_number = Column(SmallInteger,  nullable=False)
    question_text   = Column(Text,          nullable=False)
    student_answer  = Column(Text,          nullable=True)
    ai_reasoning    = Column(Text,          nullable=True)   # AI justification shown in UI

    max_score      = Column(Numeric(6, 2), nullable=False)
    score          = Column(Numeric(6, 2), nullable=False)   # live (possibly overridden)
    original_score = Column(Numeric(6, 2), nullable=False)   # AI-assigned, never mutated


# ══════════════════════════════════════════════════════════════════════════════
#  Table 9 — activity_log  (dashboard.html)
# ══════════════════════════════════════════════════════════════════════════════

class ActivityLog(Base):
    __tablename__ = "activity_log"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    # Nullable FK: users table may not be populated while system is bootstrapping
    user_id     = Column(Integer,
                         ForeignKey("users.id", ondelete="SET NULL"),
                         nullable=True, index=True)

    type        = Column(String(50),  nullable=False)
    description = Column(String(500), nullable=False)
    created_at  = Column(DateTime,    nullable=False, server_default=func.now())


# ══════════════════════════════════════════════════════════════════════════════
#  Table 10 — notifications  (dashboard.html)
# ══════════════════════════════════════════════════════════════════════════════

class Notification(Base):
    __tablename__ = "notifications"

    id      = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer,
                     ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=True, index=True)

    title   = Column(String(255), nullable=False)
    message = Column(Text,        nullable=False)
    # type mirrors localStorage values: "info" | "success" | "warning" | "error"
    type    = Column(String(20),  nullable=False, default="info")
    is_read = Column(Boolean,     nullable=False, default=False)

    created_at = Column(DateTime, nullable=False, server_default=func.now())


# ══════════════════════════════════════════════════════════════════════════════
#  Table 11 — student_extractions
#
#  One row per student answer file processed through the extraction pipeline.
#  Marks fields from the notebook output are intentionally NOT stored here —
#  marks remain manual in the UI.
# ══════════════════════════════════════════════════════════════════════════════

class StudentExtraction(Base):
    __tablename__ = "student_extractions"
    __table_args__ = (
        # Prevents a student from appearing twice in the same exam.
        # MySQL treats NULL as non-equal in UNIQUE indexes, so NULL student_id_num
        # values are still allowed (students without a readable ID on their cover page).
        UniqueConstraint('exam_id', 'student_id_num', name='uq_student_extractions_exam_student'),
    )

    id             = Column(Integer, primary_key=True, autoincrement=True)
    exam_id        = Column(Integer,
                            ForeignKey("exams.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    answer_file_id = Column(Integer,
                            ForeignKey("answer_files.id", ondelete="SET NULL"),
                            nullable=True, index=True)

    # Student identity as read from the cover page
    student_name    = Column(String(255), nullable=True)
    student_id_num  = Column(String(50),  nullable=True)   # student's own ID number
    student_section = Column(String(50),  nullable=True)

    # Path to the per-student JSON output written by the runner
    output_json_path = Column(String(512), nullable=True)

    extracted_at = Column(DateTime, nullable=False, server_default=func.now())


# ══════════════════════════════════════════════════════════════════════════════
#  Table 12 — student_answer_items
#
#  One row per sub-question per student extraction.
#  answer_type / answer_subtype / correct_answer / mcq_key_correct_option are
#  stamped from the answer key at extraction time.
#  Marks fields (expected_marks, examiner_marks_*) are NOT stored.
# ══════════════════════════════════════════════════════════════════════════════

class StudentAnswerItem(Base):
    __tablename__ = "student_answer_items"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    extraction_id = Column(Integer,
                           ForeignKey("student_extractions.id", ondelete="CASCADE"),
                           nullable=False, index=True)

    question_id     = Column(String(30),  nullable=False)   # e.g. "Q1a", "Q2"
    outcome_number  = Column(Integer,     nullable=True)

    # Student's response (transcription only — no examiner marks stored)
    student_answer          = Column(Text,      nullable=True)
    student_selected_option = Column(String(5), nullable=True)  # MCQ: "a", "b", …
    mcq_option_marks        = Column(Text,      nullable=True)  # JSON dict or NULL

    # Drawing
    has_drawing         = Column(Boolean,     nullable=False, default=False)
    drawing_description = Column(Text,        nullable=True)
    drawing_file_path   = Column(String(512), nullable=True)

    # Answer key stamp (set at extraction time from answer_key_structured.json)
    answer_type            = Column(String(100), nullable=True)
    answer_subtype         = Column(String(100), nullable=True)
    correct_answer         = Column(Text,        nullable=True)
    mcq_key_correct_option = Column(String(5),   nullable=True)


# ══════════════════════════════════════════════════════════════════════════════
#  Table 13 — model_grading_results
#
#  Stores the raw grading output from each AI model for each student answer.
#  Created by the grading engine; never mutated after creation.
#
#  phase values:
#    individual         — model graded independently (Individual Mode)
#    consensus_initial  — model's first-pass score before seeing other examiners
#    consensus_revised  — model's score after reviewing other examiners' reasoning
# ══════════════════════════════════════════════════════════════════════════════

class GradingPhase(str, enum.Enum):
    individual         = "individual"
    consensus_initial  = "consensus_initial"
    consensus_revised  = "consensus_revised"
    deterministic      = "deterministic"   # MCQ / True-False rule-based grade


class ModelGradingResult(Base):
    __tablename__ = "model_grading_results"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    grading_job_id = Column(Integer,
                            ForeignKey("grading_jobs.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    extraction_id  = Column(Integer,
                            ForeignKey("student_extractions.id", ondelete="CASCADE"),
                            nullable=False, index=True)

    question_id    = Column(String(30),  nullable=False, index=True)   # "Q1a", "Q2", …

    # Which model/examiner produced this result
    model_name     = Column(String(50),  nullable=False)   # "claude" | "qwen" | "deepseek"
    examiner_label = Column(String(20),  nullable=True)    # "Examiner 1" … (consensus only)
    phase          = Column(Enum(GradingPhase), nullable=False,
                            default=GradingPhase.individual)

    # Scores
    assigned_score = Column(Numeric(6, 2), nullable=True)
    max_score      = Column(Numeric(6, 2), nullable=False)

    # Grading detail
    basis_used     = Column(String(30),  nullable=True)    # "rubric" | "answer_key" | "both"
    reason         = Column(Text,        nullable=True)
    matched_points = Column(Text,        nullable=True)    # JSON array string
    missing_points = Column(Text,        nullable=True)    # JSON array string

    # For consensus_revised: did the examiner change their score?
    score_changed     = Column(Boolean, nullable=True)
    revision_reason   = Column(Text,    nullable=True)

    created_at = Column(DateTime, nullable=False, server_default=func.now())


# ══════════════════════════════════════════════════════════════════════════════
#  Table 14 — grading_final_scores
#
#  One row per (grading_job_id, extraction_id, question_id).
#  Holds the consolidated final score after all model passes.
#  This is the authoritative score used to populate exam_results and
#  student_question_scores.
# ══════════════════════════════════════════════════════════════════════════════

class GradingFinalScore(Base):
    __tablename__ = "grading_final_scores"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    grading_job_id = Column(Integer,
                            ForeignKey("grading_jobs.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    extraction_id  = Column(Integer,
                            ForeignKey("student_extractions.id", ondelete="CASCADE"),
                            nullable=False, index=True)

    question_id    = Column(String(30),  nullable=False, index=True)
    final_score    = Column(Numeric(6, 2), nullable=False)
    max_score      = Column(Numeric(6, 2), nullable=False)

    # Grading mode used for this question
    grading_mode   = Column(String(30),  nullable=True)   # "individual" | "consensus" | "deterministic"
    # Reasoning shown in the UI (summary from winning model or averaged reason)
    reasoning_summary = Column(Text,    nullable=True)
    matched_points    = Column(Text,    nullable=True)    # JSON array string
    missing_points    = Column(Text,    nullable=True)    # JSON array string

    created_at = Column(DateTime, nullable=False, server_default=func.now())


# ══════════════════════════════════════════════════════════════════════════════
#  Table 15 — exam_gap_analysis
#
#  One row per exam.  Stores the cached Qwen gap analysis JSON so the learning-
#  gaps page loads instantly without re-calling the LLM on every page visit.
#  Written automatically at the end of every grading job.
#  The UNIQUE constraint on exam_id enforces at-most-one cached result per exam.
# ══════════════════════════════════════════════════════════════════════════════

class ExamGapAnalysis(Base):
    __tablename__ = "exam_gap_analysis"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    exam_id       = Column(Integer,
                           ForeignKey("exams.id", ondelete="CASCADE"),
                           nullable=False, unique=True, index=True)

    analysis_json = Column(Text,        nullable=False)   # full JSON blob from run_gap_analysis()
    model_name    = Column(String(50),  nullable=False, default="qwen")
    generated_at  = Column(DateTime,    nullable=False,
                           server_default=func.now(), onupdate=func.now())


# ══════════════════════════════════════════════════════════════════════════════
#  Table 16 — grading_reports
#
#  One row per grading run result.  Each completed grading job produces one
#  report record.  Multiple versions can exist per exam; exactly one may be
#  "active" at a time (the one whose results are the "official" view).
#
#  status values:
#    draft    — created after grading but not yet activated by instructor
#    active   — currently active / approved report for this exam
#    replaced — was active; superseded by a newer report
# ══════════════════════════════════════════════════════════════════════════════

class GradingReport(Base):
    __tablename__ = "grading_reports"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    exam_id        = Column(Integer,
                            ForeignKey("exams.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    grading_job_id = Column(Integer,
                            ForeignKey("grading_jobs.id", ondelete="SET NULL"),
                            nullable=True, index=True)

    version_number = Column(Integer,      nullable=False)          # sequential per exam (1, 2, 3, …)
    scope          = Column(String(20),   nullable=False,
                            default="all")                         # "all" | "selected"
    student_count  = Column(Integer,      nullable=True)           # students graded in this run
    average_score  = Column(Numeric(5, 2), nullable=True)          # avg % score at report creation time

    # "draft" | "active" | "replaced"
    status         = Column(String(20),   nullable=False, default="draft")
    is_active      = Column(Boolean,      nullable=False, default=False)

    created_at     = Column(DateTime,     nullable=False, server_default=func.now())
    approved_at    = Column(DateTime,     nullable=True)           # set when status → active
