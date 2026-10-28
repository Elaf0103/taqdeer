"""
Pydantic schemas for the Taqdeer API.

Field aliases match the frontend's localStorage object shape so that
the JSON coming in and going out requires minimal mapping on the JS side.

Frontend field  →  alias used here  →  SQLAlchemy column
─────────────────────────────────────────────────────────
dept            →  dept              →  department
students        →  students          →  student_count
totalMarks      →  totalMarks        →  total_marks
answerFiles     →  answer_files      →  answer_files_count   (instructor-uploaded PDFs)
graded          →  graded            →  graded_count
avgScore        →  avgScore          →  avg_score
"""

from __future__ import annotations

import json
from datetime import date as _Date, datetime
from typing import List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

from models import Department, ExamStatus, ExamType, GradeStatus, ProfessionalTitle, Semester, UserRole


# ── Shared config ─────────────────────────────────────────────────────────────

class _AliasedModel(BaseModel):
    """Base class: serialise/deserialise using the declared aliases."""
    model_config = ConfigDict(populate_by_name=True, from_attributes=True)


# ── Request schemas ───────────────────────────────────────────────────────────

class ExamCreate(_AliasedModel):
    """Body for POST /exams.  Field names match the frontend form payload."""
    name:        str      = Field(..., min_length=3, max_length=255)
    type:        ExamType
    semester:    Semester
    date:        _Date
    section:     str      = Field(..., min_length=1, max_length=50)
    dept:        Department = Field(..., alias="dept")
    students:    int      = Field(..., alias="students",    ge=1,  le=999)
    total_marks: int      = Field(..., alias="totalMarks", ge=1,  le=1000)


class ExamUpdate(_AliasedModel):
    """Body for PUT /exams/{id}.  All fields are optional (partial update)."""
    name:        Optional[str]        = Field(None, min_length=3, max_length=255)
    type:        Optional[ExamType]   = None
    semester:    Optional[Semester]   = None
    date:        Optional[_Date]      = None
    section:     Optional[str]        = Field(None, min_length=1, max_length=50)
    dept:        Optional[Department] = Field(None, alias="dept")
    students:    Optional[int]        = Field(None, alias="students",    ge=1, le=999)
    total_marks: Optional[int]        = Field(None, alias="totalMarks",  ge=1, le=1000)


# ── Response schema ───────────────────────────────────────────────────────────

class ExamRead(_AliasedModel):
    """
    Response shape for GET /exams and GET /exams/{id}.
    Aliases mirror the field names the frontend already reads from localStorage.
    """
    id:      int
    name:    str
    type:    ExamType
    date:    _Date
    section: str

    # Aliased to match frontend names
    dept:        Department = Field(serialization_alias="dept",        validation_alias="dept")
    students:    int        = Field(serialization_alias="students",    validation_alias="students")
    total_marks: int        = Field(serialization_alias="totalMarks",  validation_alias="totalMarks")
    answer_files: int       = Field(serialization_alias="answerFiles",  validation_alias="answerFiles")
    graded:      int        = Field(serialization_alias="graded",      validation_alias="graded")
    avg_score:   float      = Field(serialization_alias="avgScore",    validation_alias="avgScore")

    semester:      Optional[Semester] = None
    status:        ExamStatus
    workflow_step: int  = Field(serialization_alias="workflowStep",  validation_alias="workflowStep")
    is_archived:   bool = Field(serialization_alias="isArchived",    validation_alias="isArchived")
    created_at:    datetime
    updated_at:    datetime

    @classmethod
    def from_orm(cls, obj) -> "ExamRead":
        """Map SQLAlchemy column names → Pydantic field names before validation."""
        return cls.model_validate({
            "id":            obj.id,
            "name":          obj.name,
            "type":          obj.type,
            "semester":      obj.semester,
            "date":          obj.date,
            "section":       obj.section,
            "dept":          obj.department,
            "students":      obj.student_count,
            "totalMarks":    obj.total_marks,
            "answerFiles":   obj.answer_files_count,
            "graded":        obj.graded_count,
            "avgScore":      float(obj.avg_score),
            "status":        obj.status,
            "workflowStep":  obj.workflow_step,
            "isArchived":    bool(obj.is_archived),
            "created_at":    obj.created_at,
            "updated_at":    obj.updated_at,
        })


# ── Rubric schemas ─────────────────────────────────────────────────────────────

class RubricCreate(_AliasedModel):
    """Body for POST /rubrics."""
    exam_id:   int = Field(..., alias="examId",   gt=0)
    file_name: str = Field(..., alias="fileName", min_length=1, max_length=255)
    file_size: int = Field(..., alias="fileSize", gt=0)
    file_path: Optional[str] = Field(None, alias="filePath")


class RubricUpdate(_AliasedModel):
    """Body for PUT /rubrics/{id}.  All fields optional (partial update)."""
    file_name: Optional[str] = Field(None, alias="fileName", min_length=1, max_length=255)
    file_size: Optional[int] = Field(None, alias="fileSize", gt=0)
    file_path: Optional[str] = Field(None, alias="filePath")


class RubricRead(_AliasedModel):
    exam_id:    int      = Field(serialization_alias="examId",   validation_alias="examId")
    file_name:  str      = Field(serialization_alias="fileName", validation_alias="fileName")
    file_size:  int      = Field(serialization_alias="fileSize", validation_alias="fileSize")
    file_path:  Optional[str] = Field(None, serialization_alias="filePath", validation_alias="filePath")
    id:         int
    uploaded_at: datetime = Field(serialization_alias="uploadedAt", validation_alias="uploadedAt")

    @classmethod
    def from_orm(cls, obj) -> "RubricRead":
        return cls.model_validate({
            "id":          obj.id,
            "examId":      obj.exam_id,
            "fileName":    obj.file_name,
            "fileSize":    obj.file_size,
            "filePath":    obj.file_path,
            "uploadedAt":  obj.uploaded_at,
        })


# ── AnswerFile schemas ─────────────────────────────────────────────────────────

class AnswerFileRead(_AliasedModel):
    """Response shape for GET /answer-files and POST /answer-files."""
    id:                int
    exam_id:           int           = Field(serialization_alias="examId",           validation_alias="examId")
    file_name:         str           = Field(serialization_alias="fileName",          validation_alias="fileName")
    file_size:         int           = Field(serialization_alias="fileSize",          validation_alias="fileSize")
    file_path:         Optional[str] = Field(None, serialization_alias="filePath",    validation_alias="filePath")
    uploaded_at:       datetime      = Field(serialization_alias="uploadedAt",        validation_alias="uploadedAt")
    extraction_status: Optional[str] = Field(None, serialization_alias="extractionStatus", validation_alias="extractionStatus")
    extraction_error:  Optional[str] = Field(None, serialization_alias="extractionError",  validation_alias="extractionError")

    @classmethod
    def from_orm(cls, obj) -> "AnswerFileRead":
        return cls.model_validate({
            "id":               obj.id,
            "examId":           obj.exam_id,
            "fileName":         obj.file_name,
            "fileSize":         obj.file_size,
            "filePath":         obj.file_path,
            "uploadedAt":       obj.uploaded_at,
            "extractionStatus": obj.extraction_status.value if obj.extraction_status else None,
            "extractionError":  obj.extraction_error,
        })


# ── ModelAnswer schemas ────────────────────────────────────────────────────────

class ModelAnswerCreate(_AliasedModel):
    """Body for POST /model-answers."""
    exam_id:         int   = Field(..., alias="examId",         gt=0)
    question_number: int   = Field(..., alias="questionNumber", ge=1, le=999)
    question_text:   str   = Field(..., alias="questionText",   min_length=1)
    model_answer:    str   = Field(..., alias="modelAnswer",    min_length=1)
    score:           float = Field(...,                         gt=0, le=999.99)
    answer_type:     str   = Field("recall_general", alias="questionType", max_length=50)


class ModelAnswerUpdate(_AliasedModel):
    """Body for PUT /model-answers/{id}.  All fields optional (partial update)."""
    question_number: Optional[int]   = Field(None, alias="questionNumber", ge=1, le=999)
    question_text:   Optional[str]   = Field(None, alias="questionText",   min_length=1)
    model_answer:    Optional[str]   = Field(None, alias="modelAnswer",    min_length=1)
    score:           Optional[float] = Field(None,                         gt=0, le=999.99)
    answer_type:     Optional[str]   = Field(None, alias="questionType",   max_length=50)


class ModelAnswerRead(_AliasedModel):
    id:              int
    exam_id:         int   = Field(serialization_alias="examId",         validation_alias="examId")
    question_number: int   = Field(serialization_alias="questionNumber", validation_alias="questionNumber")
    question_text:   str   = Field(serialization_alias="questionText",   validation_alias="questionText")
    model_answer:    str   = Field(serialization_alias="modelAnswer",    validation_alias="modelAnswer")
    score:           float
    answer_type:     str   = Field("recall_general", serialization_alias="questionType", validation_alias="questionType")
    # Diagram answer key image paths extracted during the extraction pipeline.
    # Empty list for non-diagrammatic questions; one or more paths for diagram questions.
    answer_key_images: List[str] = Field(
        default_factory=list,
        serialization_alias="answerKeyImages",
        validation_alias="answerKeyImages",
    )
    created_at:      datetime = Field(serialization_alias="createdAt",   validation_alias="createdAt")
    updated_at:      datetime = Field(serialization_alias="updatedAt",   validation_alias="updatedAt")

    @classmethod
    def from_orm(cls, obj) -> "ModelAnswerRead":
        # Parse answer_key_images from the JSON column; default to empty list
        raw_images = obj.answer_key_images
        try:
            parsed_images = json.loads(raw_images) if raw_images else []
            if not isinstance(parsed_images, list):
                parsed_images = []
        except (json.JSONDecodeError, TypeError, ValueError):
            parsed_images = []

        return cls.model_validate({
            "id":               obj.id,
            "examId":           obj.exam_id,
            "questionNumber":   obj.question_number,
            "questionText":     obj.question_text,
            "modelAnswer":      obj.model_answer,
            "score":            float(obj.score),
            "questionType":     obj.answer_type or "recall_general",
            "answerKeyImages":  parsed_images,
            "createdAt":        obj.created_at,
            "updatedAt":        obj.updated_at,
        })


# ── ExamResult schemas ─────────────────────────────────────────────────────────

class ExamResultRead(_AliasedModel):
    """Response shape for GET /results and GET /results/{id}."""
    id:              int
    exam_id:         int            = Field(serialization_alias="examId",        validation_alias="examId")
    student_id:      str            = Field(serialization_alias="studentId",     validation_alias="studentId")
    student_name:    str            = Field(serialization_alias="studentName",   validation_alias="studentName")
    total_score:     float          = Field(serialization_alias="totalScore",    validation_alias="totalScore")
    grade:           str
    status:          GradeStatus
    manually_edited: bool           = Field(serialization_alias="manuallyEdited", validation_alias="manuallyEdited")
    created_at:      datetime       = Field(serialization_alias="createdAt",     validation_alias="createdAt")
    updated_at:      datetime       = Field(serialization_alias="updatedAt",     validation_alias="updatedAt")

    @classmethod
    def from_orm(cls, obj) -> "ExamResultRead":
        return cls.model_validate({
            "id":             obj.id,
            "examId":         obj.exam_id,
            "studentId":      obj.student_id,
            "studentName":    obj.student_name,
            "totalScore":     float(obj.total_score),
            "grade":          obj.grade or "F",
            "status":         obj.status,
            "manuallyEdited": obj.manually_edited,
            "createdAt":      obj.created_at,
            "updatedAt":      obj.updated_at,
        })


class ExamResultUpdate(_AliasedModel):
    """Body for PUT /results/{id}.  All fields optional (partial update)."""
    total_score: Optional[float] = Field(None, alias="totalScore", ge=0)


class ResultStatsRead(_AliasedModel):
    """Aggregate statistics returned by GET /results/stats."""
    total:         int
    graded:        int
    pending:       int
    approved:      int
    avg_score:     float = Field(serialization_alias="avgScore",     validation_alias="avgScore")
    highest_score: float = Field(serialization_alias="highestScore", validation_alias="highestScore")
    lowest_score:  float = Field(serialization_alias="lowestScore",  validation_alias="lowestScore")
    pass_rate:     float = Field(serialization_alias="passRate",     validation_alias="passRate")


# ── StudentQuestionScore schemas ───────────────────────────────────────────────

class StudentQuestionScoreRead(_AliasedModel):
    """Response shape for GET /results/{id}/question-scores."""
    id:              int
    result_id:       int           = Field(serialization_alias="resultId",       validation_alias="resultId")
    question_number: int           = Field(serialization_alias="questionNumber", validation_alias="questionNumber")
    question_text:   str           = Field(serialization_alias="questionText",   validation_alias="questionText")
    student_answer:  Optional[str] = Field(None, serialization_alias="studentAnswer", validation_alias="studentAnswer")
    ai_reasoning:    Optional[str] = Field(None, serialization_alias="aiReasoning",   validation_alias="aiReasoning")
    max_score:       float         = Field(serialization_alias="maxScore",       validation_alias="maxScore")
    score:           float
    original_score:  float         = Field(serialization_alias="originalScore",  validation_alias="originalScore")

    @classmethod
    def from_orm(cls, obj) -> "StudentQuestionScoreRead":
        return cls.model_validate({
            "id":             obj.id,
            "resultId":       obj.result_id,
            "questionNumber": obj.question_number,
            "questionText":   obj.question_text,
            "studentAnswer":  obj.student_answer,
            "aiReasoning":    obj.ai_reasoning,
            "maxScore":       float(obj.max_score),
            "score":          float(obj.score),
            "originalScore":  float(obj.original_score),
        })


class StudentQuestionScoreUpdate(_AliasedModel):
    """Body for PUT /results/{result_id}/question-scores/{score_id}."""
    score: float = Field(..., ge=0)


# ── Seed schema ────────────────────────────────────────────────────────────────

class ResultSeedPayload(_AliasedModel):
    """Body for POST /results/seed.  Dev / testing use only."""
    exam_id:       int           = Field(..., alias="examId", gt=0)
    student_count: Optional[int] = Field(None, alias="studentCount", ge=1, le=500)


# ── Auth / User schemas ─────────────────────────────────────────────────────────

class UserRegister(_AliasedModel):
    """Body for POST /auth/register."""
    full_name:          str                          = Field(..., alias="fullName",          min_length=2, max_length=255)
    email:              str                          = Field(...,                             min_length=5, max_length=255)
    password:           str                          = Field(...,                             min_length=8, max_length=128)
    department:         Optional[Department]         = None
    professional_title: Optional[ProfessionalTitle] = Field(None, alias="professionalTitle")


class UserLogin(_AliasedModel):
    """Body for POST /auth/login."""
    email:    str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class UserRead(_AliasedModel):
    """
    Response shape for /auth and /users endpoints.
    Mirrors the localStorage keys the frontend already uses:
      fullName, avatarPath, createdAt, updatedAt
    """
    id:                 int
    full_name:          str                          = Field(serialization_alias="fullName",          validation_alias="fullName")
    email:              str
    department:         Optional[Department]         = None
    role:               UserRole
    professional_title: Optional[ProfessionalTitle] = Field(None, serialization_alias="professionalTitle", validation_alias="professionalTitle")
    avatar_path:        Optional[str]               = Field(None, serialization_alias="avatarPath",   validation_alias="avatarPath")
    created_at:         datetime                     = Field(serialization_alias="createdAt",          validation_alias="createdAt")
    updated_at:         datetime                     = Field(serialization_alias="updatedAt",          validation_alias="updatedAt")

    @classmethod
    def from_orm(cls, obj) -> "UserRead":
        return cls.model_validate({
            "id":                obj.id,
            "fullName":          obj.full_name,
            "email":             obj.email,
            "department":        obj.department,
            "role":              obj.role,
            "professionalTitle": obj.professional_title,
            "avatarPath":        obj.avatar_path,
            "createdAt":         obj.created_at,
            "updatedAt":         obj.updated_at,
        })


class UserAdminRead(_AliasedModel):
    """
    Response shape for GET /admin/users.
    Includes is_active for activate/deactivate UI.
    """
    id:         int
    full_name:  str              = Field(serialization_alias="fullName",   validation_alias="fullName")
    email:      str
    department: Optional[Department] = None
    role:       UserRole
    is_active:  bool             = Field(serialization_alias="isActive",   validation_alias="isActive")
    created_at: datetime         = Field(serialization_alias="createdAt",  validation_alias="createdAt")

    @classmethod
    def from_orm(cls, obj) -> "UserAdminRead":
        return cls.model_validate({
            "id":         obj.id,
            "fullName":   obj.full_name,
            "email":      obj.email,
            "department": obj.department,
            "role":       obj.role,
            "isActive":   obj.is_active,
            "createdAt":  obj.created_at,
        })


class ActivityLogRead(_AliasedModel):
    """Response shape for GET /activity/."""
    id:          int
    type:        str
    description: str
    created_at:  datetime = Field(serialization_alias="createdAt", validation_alias="createdAt")

    @classmethod
    def from_orm(cls, obj) -> "ActivityLogRead":
        return cls.model_validate({
            "id":          obj.id,
            "type":        obj.type,
            "description": obj.description,
            "createdAt":   obj.created_at,
        })


class NotificationRead(_AliasedModel):
    """Response shape for GET /notifications/."""
    id:         int
    title:      str
    message:    str
    type:       str
    is_read:    bool     = Field(serialization_alias="isRead",    validation_alias="isRead")
    created_at: datetime = Field(serialization_alias="createdAt", validation_alias="createdAt")

    @classmethod
    def from_orm(cls, obj) -> "NotificationRead":
        return cls.model_validate({
            "id":        obj.id,
            "title":     obj.title,
            "message":   obj.message,
            "type":      obj.type,
            "isRead":    obj.is_read,
            "createdAt": obj.created_at,
        })


class UserUpdate(_AliasedModel):
    """Body for PUT /users/me.  All fields optional (partial update)."""
    full_name:          Optional[str]                = Field(None, alias="fullName",          min_length=2, max_length=255)
    department:         Optional[Department]         = None
    professional_title: Optional[ProfessionalTitle] = Field(None, alias="professionalTitle")


class LoginResponse(_AliasedModel):
    """Response shape for POST /auth/login."""
    access_token: str      = Field(serialization_alias="accessToken")
    token_type:   str      = Field(default="bearer", serialization_alias="tokenType")
    user:         UserRead


class RequestResetPayload(_AliasedModel):
    """Body for POST /auth/request-reset."""
    email: str = Field(..., min_length=1)


class ForgotPasswordPayload(_AliasedModel):
    """Body for POST /auth/forgot-password."""
    email: str = Field(..., min_length=1)


class VerifyResetCodePayload(_AliasedModel):
    """Body for POST /auth/verify-reset-code."""
    email: str = Field(..., min_length=1)
    code:  str = Field(..., min_length=1)


class ResetPasswordPayload(_AliasedModel):
    """
    Body for POST /auth/reset-password.

    Supports two flows:
      • Token-link flow (new):   provide ``token`` + ``newPassword``
      • Code flow (legacy):      provide ``email`` + ``code`` + ``newPassword``
    """
    # Token-link flow
    token: Optional[str] = None
    # Legacy code flow
    email: Optional[str] = None
    code:  Optional[str] = None
    new_password: str = Field(..., alias="newPassword", min_length=8, max_length=128)


# ── Extraction schemas ─────────────────────────────────────────────────────────

class StudentExtractionRead(_AliasedModel):
    """Response shape for GET /extraction/students/{exam_id}."""
    id:              int
    exam_id:         int            = Field(serialization_alias="examId",         validation_alias="examId")
    answer_file_id:  Optional[int]  = Field(None, serialization_alias="answerFileId", validation_alias="answerFileId")
    student_name:    Optional[str]  = Field(None, serialization_alias="studentName",  validation_alias="studentName")
    student_id_num:  Optional[str]  = Field(None, serialization_alias="studentIdNum", validation_alias="studentIdNum")
    student_section: Optional[str]  = Field(None, serialization_alias="studentSection", validation_alias="studentSection")
    extracted_at:    datetime       = Field(serialization_alias="extractedAt",    validation_alias="extractedAt")

    @classmethod
    def from_orm(cls, obj) -> "StudentExtractionRead":
        return cls.model_validate({
            "id":              obj.id,
            "examId":          obj.exam_id,
            "answerFileId":    obj.answer_file_id,
            "studentName":     obj.student_name,
            "studentIdNum":    obj.student_id_num,
            "studentSection":  obj.student_section,
            "extractedAt":     obj.extracted_at,
        })


class StudentAnswerItemRead(_AliasedModel):
    """Response shape for a single student answer item."""
    id:                      int
    extraction_id:           int            = Field(serialization_alias="extractionId",          validation_alias="extractionId")
    question_id:             str            = Field(serialization_alias="questionId",             validation_alias="questionId")
    outcome_number:          Optional[int]  = Field(None, serialization_alias="outcomeNumber",    validation_alias="outcomeNumber")
    student_answer:          Optional[str]  = Field(None, serialization_alias="studentAnswer",    validation_alias="studentAnswer")
    student_selected_option: Optional[str]  = Field(None, serialization_alias="studentSelectedOption", validation_alias="studentSelectedOption")
    mcq_option_marks:        Optional[str]  = Field(None, serialization_alias="mcqOptionMarks",   validation_alias="mcqOptionMarks")
    has_drawing:             bool           = Field(serialization_alias="hasDrawing",             validation_alias="hasDrawing")
    drawing_description:     Optional[str]  = Field(None, serialization_alias="drawingDescription", validation_alias="drawingDescription")
    drawing_file_path:       Optional[str]  = Field(None, serialization_alias="drawingFilePath",  validation_alias="drawingFilePath")
    answer_type:             Optional[str]  = Field(None, serialization_alias="answerType",       validation_alias="answerType")
    answer_subtype:          Optional[str]  = Field(None, serialization_alias="answerSubtype",    validation_alias="answerSubtype")
    correct_answer:          Optional[str]  = Field(None, serialization_alias="correctAnswer",    validation_alias="correctAnswer")
    mcq_key_correct_option:  Optional[str]  = Field(None, serialization_alias="mcqKeyCorrectOption", validation_alias="mcqKeyCorrectOption")

    @classmethod
    def from_orm(cls, obj) -> "StudentAnswerItemRead":
        return cls.model_validate({
            "id":                     obj.id,
            "extractionId":           obj.extraction_id,
            "questionId":             obj.question_id,
            "outcomeNumber":          obj.outcome_number,
            "studentAnswer":          obj.student_answer,
            "studentSelectedOption":  obj.student_selected_option,
            "mcqOptionMarks":         obj.mcq_option_marks,
            "hasDrawing":             bool(obj.has_drawing),
            "drawingDescription":     obj.drawing_description,
            "drawingFilePath":        obj.drawing_file_path,
            "answerType":             obj.answer_type,
            "answerSubtype":          obj.answer_subtype,
            "correctAnswer":          obj.correct_answer,
            "mcqKeyCorrectOption":    obj.mcq_key_correct_option,
        })


class StudentExtractionWithAnswers(_AliasedModel):
    """
    Full extraction record with answer items.
    Returned by GET /extraction/students/{exam_id}/{extraction_id}/answers.
    """
    extraction: StudentExtractionRead
    answers:    list[StudentAnswerItemRead]


class StudentAnswerItemUpdate(_AliasedModel):
    """
    Body for PATCH /extraction/answers/{item_id}.
    All fields are optional — only provided fields are written.
    Open-ended answers: supply studentAnswer.
    MCQ / True-False: supply studentSelectedOption and/or mcqOptionMarks.
    """
    student_answer:          Optional[str] = Field(None, alias="studentAnswer",         max_length=8000)
    student_selected_option: Optional[str] = Field(None, alias="studentSelectedOption", max_length=10)
    mcq_option_marks:        Optional[str] = Field(None, alias="mcqOptionMarks",        max_length=4000)


class AnswerKeyExtractionResponse(_AliasedModel):
    """Response shape for POST /extraction/answer-key/{exam_id}."""
    message:           str
    questions_queued:  int = Field(serialization_alias="questionsQueued", validation_alias="questionsQueued")
