from pathlib import Path

from dotenv import load_dotenv
# Use an explicit path so load_dotenv() always finds backend/.env regardless
# of where uvicorn is started from.  override=True ensures values in .env
# always win over any empty / stale env-var already in the shell environment.
load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=True)

# ── Startup: verify API key loading ───────────────────────────────────────────
import os as _os
def _key_status(env_var: str) -> str:
    v = _os.environ.get(env_var, "")
    if not v:
        return "MISSING"
    # Show only length + first-4 + last-4 — never the full key
    return f"ok (len={len(v)}, {v[:4]}…{v[-4:]})"

print("[startup] ── API key status ────────────────────────────────────────")
print(f"[startup] ANTHROPIC_API_KEY : {_key_status('ANTHROPIC_API_KEY')}")
print(f"[startup] OPENAI_API_KEY    : {_key_status('OPENAI_API_KEY')}")
print(f"[startup] OPENAI_BASE_URL   : {_os.environ.get('OPENAI_BASE_URL', '(not set — using api.openai.com)')}")
print(f"[startup] QWEN_API_KEY      : {_key_status('QWEN_API_KEY')}")
print(f"[startup] DEEPSEEK_API_KEY  : {_key_status('DEEPSEEK_API_KEY')}")
print(f"[startup] EMAIL_PROVIDER   : {_os.environ.get('EMAIL_PROVIDER', 'resend (default)')}")
print(f"[startup] EMAIL_API_KEY   : {_key_status('EMAIL_API_KEY')}")
print(f"[startup] MAIL_FROM       : {_os.environ.get('MAIL_FROM', '(not set)')}")
print("[startup] ───────────────────────────────────────────────────────────")

# ── Startup: SMTP / reset-password diagnostics ────────────────────────────────
import os as _os2
_smtp_host  = _os2.environ.get("SMTP_HOST",     "")
_smtp_port  = _os2.environ.get("SMTP_PORT",     "")
_smtp_user  = _os2.environ.get("SMTP_USER",     "")
_smtp_pass  = _os2.environ.get("SMTP_PASSWORD", "")
_mail_from  = _os2.environ.get("MAIL_FROM",     "")
_app_env    = _os2.environ.get("APP_ENV",        "development")
_fe_url     = _os2.environ.get("FRONTEND_URL",  "")
_env_file   = str(Path(__file__).parent / ".env")

print("[ENV] ── Reset-password / SMTP configuration ─────────────────────────")
print(f"[ENV] dotenv path            = {_env_file}")
print(f"[ENV] APP_ENV                = {_app_env!r}")
print(f"[ENV] SMTP_HOST              = {_smtp_host!r}")
print(f"[ENV] SMTP_PORT              = {_smtp_port!r}")
print(f"[ENV] SMTP_USER              = {_smtp_user!r}")
print(f"[ENV] SMTP_PASSWORD_PRESENT  = {'YES' if _smtp_pass else 'NO — SMTP_PASSWORD is not set!'}")
print(f"[ENV] MAIL_FROM              = {_mail_from!r}")
print(f"[ENV] FRONTEND_URL           = {_fe_url!r}")

# Sanity checks
_env_warnings = []
if not _smtp_host:
    _env_warnings.append("SMTP_HOST is missing — email will not be sent")
if not _smtp_user:
    _env_warnings.append("SMTP_USER is missing — email will not be sent")
if not _smtp_pass:
    _env_warnings.append("SMTP_PASSWORD is missing — email will not be sent")
if _app_env.lower() != "production":
    _env_warnings.append(
        f"APP_ENV={_app_env!r} — dev_code will be returned in forgot-password responses "
        "(set APP_ENV=production to disable)"
    )
for _w in _env_warnings:
    print(f"[ENV] ⚠  {_w}")
if not _env_warnings:
    print("[ENV] ✓  All SMTP variables present — email sending should work")
print("[ENV] ──────────────────────────────────────────────────────────────────")

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from database import engine, SessionLocal
from models import Base, User, UserRole
from security import hash_password
from routers import auth, exams, rubrics, answer_files, results, users, admin, activity, notifications, extraction, grading, gap_analysis, reports, reviewed_export

# ── Startup: create tables ─────────────────────────────────────────────────────
Base.metadata.create_all(bind=engine)


# ── Startup: idempotent column migration ──────────────────────────────────────
def _migrate_add_owner_id() -> None:
    """
    Add owner_id to exams if it doesn't exist yet.
    create_all() only creates missing tables, not missing columns, so we need
    this for databases that already had the exams table before this column
    was added to the model.
    """
    with engine.connect() as conn:
        exists = conn.execute(text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() "
            "  AND table_name   = 'exams' "
            "  AND column_name  = 'owner_id'"
        )).scalar()
        if not exists:
            conn.execute(text(
                "ALTER TABLE exams "
                "ADD COLUMN owner_id INT NULL, "
                "ADD CONSTRAINT fk_exams_owner "
                "  FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE SET NULL"
            ))
            conn.commit()

_migrate_add_owner_id()


def _migrate_add_is_active() -> None:
    """Add is_active to users if it doesn't exist yet (safe to run on every startup)."""
    with engine.connect() as conn:
        exists = conn.execute(text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() "
            "  AND table_name   = 'users' "
            "  AND column_name  = 'is_active'"
        )).scalar()
        if not exists:
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN is_active TINYINT(1) NOT NULL DEFAULT 1"
            ))
            conn.commit()

_migrate_add_is_active()


def _migrate_add_is_archived() -> None:
    """Add is_archived to exams if it doesn't exist yet (safe to run on every startup)."""
    with engine.connect() as conn:
        exists = conn.execute(text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() "
            "  AND table_name   = 'exams' "
            "  AND column_name  = 'is_archived'"
        )).scalar()
        if not exists:
            conn.execute(text(
                "ALTER TABLE exams ADD COLUMN is_archived TINYINT(1) NOT NULL DEFAULT 0, "
                "ADD INDEX idx_exams_is_archived (is_archived)"
            ))
            conn.commit()

_migrate_add_is_archived()


def _migrate_add_answer_type() -> None:
    """Add answer_type to exam_model_answers if it doesn't exist yet."""
    with engine.connect() as conn:
        exists = conn.execute(text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() "
            "  AND table_name   = 'exam_model_answers' "
            "  AND column_name  = 'answer_type'"
        )).scalar()
        if not exists:
            conn.execute(text(
                "ALTER TABLE exam_model_answers "
                "ADD COLUMN answer_type VARCHAR(50) NOT NULL DEFAULT 'recall_general'"
            ))
            conn.commit()

_migrate_add_answer_type()


def _migrate_grading_job_columns() -> None:
    """
    Add grading configuration and error columns to grading_jobs.
    Safe to run on every startup — checks column existence before altering.
    """
    new_columns = [
        ("selected_models",              "VARCHAR(255) NULL"),
        ("grading_basis",                "VARCHAR(20) NULL"),
        ("consensus_enabled",            "TINYINT(1) NULL"),
        ("general_grading_instructions", "TEXT NULL"),
        ("error_message",                "TEXT NULL"),
        ("student_ids",                  "TEXT NULL"),
    ]
    with engine.connect() as conn:
        for col_name, col_def in new_columns:
            exists = conn.execute(text(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_schema = DATABASE() "
                "  AND table_name   = 'grading_jobs' "
                f"  AND column_name  = '{col_name}'"
            )).scalar()
            if not exists:
                conn.execute(text(
                    f"ALTER TABLE grading_jobs ADD COLUMN {col_name} {col_def}"
                ))
        conn.commit()

_migrate_grading_job_columns()


def _migrate_exam_model_answer_grading_config() -> None:
    """
    Add per-question grading configuration columns to exam_model_answers.
    Safe to run on every startup.
    """
    new_columns = [
        ("scoring_method",      "TEXT NULL"),
        ("additional_checks",   "TEXT NULL"),
        ("custom_instructions", "TEXT NULL"),
    ]
    with engine.connect() as conn:
        for col_name, col_def in new_columns:
            exists = conn.execute(text(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_schema = DATABASE() "
                "  AND table_name   = 'exam_model_answers' "
                f"  AND column_name  = '{col_name}'"
            )).scalar()
            if not exists:
                conn.execute(text(
                    f"ALTER TABLE exam_model_answers ADD COLUMN {col_name} {col_def}"
                ))
        conn.commit()

_migrate_exam_model_answer_grading_config()


def _migrate_fix_student_question_scores_fk() -> None:
    """
    student_question_scores.result_id FK was originally created pointing to
    the old 'student_submissions' table.  That table was later renamed to
    'exam_results', but the FK was never updated.

    Drop the stale FK (student_question_scores_ibfk_1) and recreate it
    pointing to exam_results.id.  Safe to run on every startup.
    """
    with engine.connect() as conn:
        wrong_fk = conn.execute(text(
            "SELECT COUNT(*) FROM information_schema.KEY_COLUMN_USAGE "
            "WHERE table_schema = DATABASE() "
            "  AND table_name              = 'student_question_scores' "
            "  AND constraint_name         = 'student_question_scores_ibfk_1' "
            "  AND referenced_table_name   = 'student_submissions'"
        )).scalar()
        if wrong_fk:
            conn.execute(text(
                "ALTER TABLE student_question_scores "
                "DROP FOREIGN KEY student_question_scores_ibfk_1"
            ))
            conn.execute(text(
                "ALTER TABLE student_question_scores "
                "ADD CONSTRAINT fk_sqs_result_id "
                "FOREIGN KEY (result_id) REFERENCES exam_results(id) ON DELETE CASCADE"
            ))
            conn.commit()
            print("[migration] Repaired student_question_scores FK → exam_results")

_migrate_fix_student_question_scores_fk()


def _migrate_enforce_unique_student_per_exam() -> None:
    """
    Enforce one row per student per exam in both extraction and results tables.

    Cleanup strategy: keep the newest row (highest id) for each duplicate group.
    Cascade deletes handle child rows (student_answer_items, student_question_scores).

    Step 1 — student_extractions:
      Delete older duplicate rows where (exam_id, student_id_num) are the same.
      Then add UNIQUE(exam_id, student_id_num) if not already present.

    Step 2 — exam_results:
      Delete older duplicate rows where (exam_id, student_id) are the same.
      Then add UNIQUE(exam_id, student_id) if not already present.
    """
    with engine.connect() as conn:

        # ── Step 1a: remove duplicate student_extractions ────────────────────
        # For each group sharing (exam_id, student_id_num), keep max(id).
        # Only applies to non-NULL student_id_num values.
        conn.execute(text("""
            DELETE se1
            FROM student_extractions se1
            INNER JOIN student_extractions se2
              ON  se1.exam_id       = se2.exam_id
              AND se1.student_id_num = se2.student_id_num
              AND se1.id            < se2.id
            WHERE se1.student_id_num IS NOT NULL
        """))

        # ── Step 1b: add UNIQUE constraint on student_extractions ─────────────
        exists = conn.execute(text(
            "SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS "
            "WHERE table_schema = DATABASE() "
            "  AND table_name      = 'student_extractions' "
            "  AND constraint_name = 'uq_student_extractions_exam_student'"
        )).scalar()
        if not exists:
            conn.execute(text(
                "ALTER TABLE student_extractions "
                "ADD CONSTRAINT uq_student_extractions_exam_student "
                "UNIQUE (exam_id, student_id_num)"
            ))

        # ── Step 2a: remove duplicate exam_results ────────────────────────────
        # For each group sharing (exam_id, student_id), keep max(id).
        # student_question_scores are deleted via ON DELETE CASCADE.
        conn.execute(text("""
            DELETE er1
            FROM exam_results er1
            INNER JOIN exam_results er2
              ON  er1.exam_id    = er2.exam_id
              AND er1.student_id = er2.student_id
              AND er1.id         < er2.id
        """))

        # ── Step 2b: add UNIQUE constraint on exam_results ────────────────────
        exists = conn.execute(text(
            "SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS "
            "WHERE table_schema = DATABASE() "
            "  AND table_name      = 'exam_results' "
            "  AND constraint_name = 'uq_exam_results_exam_student'"
        )).scalar()
        if not exists:
            conn.execute(text(
                "ALTER TABLE exam_results "
                "ADD CONSTRAINT uq_exam_results_exam_student "
                "UNIQUE (exam_id, student_id)"
            ))

        conn.commit()
        print("[migration] Enforced UNIQUE(exam_id, student_id) on student_extractions and exam_results")

_migrate_enforce_unique_student_per_exam()


def _migrate_add_semester() -> None:
    """
    Add semester column and index to exams if they don't exist yet.
    Checks column and index independently so partial states are also handled.
    Safe to run on every startup.
    """
    with engine.connect() as conn:
        col_exists = conn.execute(text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() "
            "  AND table_name   = 'exams' "
            "  AND column_name  = 'semester'"
        )).scalar()

        idx_exists = conn.execute(text(
            "SELECT COUNT(*) FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() "
            "  AND table_name   = 'exams' "
            "  AND index_name   = 'idx_exams_semester'"
        )).scalar()

        if col_exists and idx_exists:
            print("[migration] semester column/index already exists, skipping")
            return

        clauses = []
        if not col_exists:
            clauses.append("ADD COLUMN semester ENUM('first','second','summer') NULL")
        if not idx_exists:
            clauses.append("ADD INDEX idx_exams_semester (semester)")

        conn.execute(text("ALTER TABLE exams " + ", ".join(clauses)))
        conn.commit()
        print("[migration] Added semester " + ("column + index" if not col_exists else "index only"))

_migrate_add_semester()


def _migrate_add_extraction_status_to_answer_files() -> None:
    """
    Add extraction_status ENUM and extraction_error TEXT to answer_files.
    Safe to run on every startup — checks column existence before altering.
    """
    with engine.connect() as conn:
        status_exists = conn.execute(text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() "
            "  AND table_name   = 'answer_files' "
            "  AND column_name  = 'extraction_status'"
        )).scalar()
        error_exists = conn.execute(text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() "
            "  AND table_name   = 'answer_files' "
            "  AND column_name  = 'extraction_error'"
        )).scalar()

        clauses = []
        if not status_exists:
            clauses.append(
                "ADD COLUMN extraction_status ENUM('pending','extracted','failed') NULL"
            )
        if not error_exists:
            clauses.append("ADD COLUMN extraction_error TEXT NULL")

        if clauses:
            conn.execute(text("ALTER TABLE answer_files " + ", ".join(clauses)))
            conn.commit()
            print("[migration] Added extraction_status/extraction_error to answer_files")

_migrate_add_extraction_status_to_answer_files()


def _migrate_add_professional_title() -> None:
    """
    Add professional_title ENUM column to users if it doesn't exist yet.
    Existing rows get NULL (displayed as no title — safe default).
    """
    with engine.connect() as conn:
        exists = conn.execute(text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() "
            "  AND table_name   = 'users' "
            "  AND column_name  = 'professional_title'"
        )).scalar()
        if not exists:
            conn.execute(text(
                "ALTER TABLE users "
                "ADD COLUMN professional_title "
                "  ENUM('dr','prof','mr','ms','mrs','miss','eng','none') NULL "
                "  DEFAULT NULL"
            ))
            conn.commit()
            print("[migration] Added professional_title column to users")

_migrate_add_professional_title()


def _migrate_grading_job_status_enum() -> None:
    """
    Extend the grading_jobs.status ENUM to include 'awaiting_confirmation'.

    MySQL ENUM columns must be explicitly altered when new values are added;
    SQLAlchemy's create_all() does NOT modify existing column definitions.

    Target values (preserves all existing ones):
        pending, running, completed, failed, awaiting_confirmation

    Safe to run on every startup — no-op if the value is already present.
    """
    with engine.connect() as conn:
        col_type: str = conn.execute(text(
            "SELECT COLUMN_TYPE FROM information_schema.columns "
            "WHERE table_schema = DATABASE() "
            "  AND table_name   = 'grading_jobs' "
            "  AND column_name  = 'status'"
        )).scalar() or ""

        if "awaiting_confirmation" in col_type:
            return  # already up-to-date, nothing to do

        print(f"[migration] grading_jobs.status ENUM before: {col_type}")

        conn.execute(text(
            "ALTER TABLE grading_jobs "
            "MODIFY COLUMN status "
            "  ENUM('pending','running','completed','failed','awaiting_confirmation') "
            "  NOT NULL DEFAULT 'pending'"
        ))
        conn.commit()

        new_type: str = conn.execute(text(
            "SELECT COLUMN_TYPE FROM information_schema.columns "
            "WHERE table_schema = DATABASE() "
            "  AND table_name   = 'grading_jobs' "
            "  AND column_name  = 'status'"
        )).scalar() or ""
        print(f"[migration] grading_jobs.status ENUM after : {new_type}")

_migrate_grading_job_status_enum()


# ── Startup: ensure the known admin account exists ────────────────────────────
def _ensure_admin() -> None:
    """
    Idempotent seed: guarantees that the admin account (ADMIN_EMAIL) exists in the
    database with role=admin.  Safe to run on every startup:
      • account absent  → created with hashed password and role=admin
      • account present, wrong role → role promoted to admin
      • account present, role=admin → no-op
    Password is only written when the account is first created; existing
    passwords are never overwritten by this function.
    """
    email    = _os.getenv("ADMIN_EMAIL", "admin@example.com")
    password = _os.getenv("ADMIN_PASSWORD", "change-me-please")
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        if user is None:
            db.add(User(
                full_name     = "Admin",
                email         = email,
                password_hash = hash_password(password),
                role          = UserRole.admin,
            ))
            db.commit()
        elif user.role != UserRole.admin:
            user.role = UserRole.admin
            db.commit()
    finally:
        db.close()

_ensure_admin()


# ── Startup: seed developer test accounts ─────────────────────────────────────
_DEV_USERS = [
    {
        "full_name": "Dev Admin",
        "email":     "admin@taqdeer.local",
        "password":  "admin123",
        "role":      UserRole.admin,
    },
    {
        "full_name": "Dev Instructor",
        "email":     "instructor@taqdeer.local",
        "password":  "instructor123",
        "role":      UserRole.instructor,
    },
]

def seed_dev_users() -> None:
    """
    Idempotent seed: ensures developer test accounts exist on every startup.
    • account absent  → created with hashed password
    • account present → no-op (password never overwritten)
    Does NOT bypass authentication or weaken security.
    """
    db = SessionLocal()
    try:
        for spec in _DEV_USERS:
            exists = db.query(User).filter(User.email == spec["email"]).first()
            if exists is None:
                db.add(User(
                    full_name     = spec["full_name"],
                    email         = spec["email"],
                    password_hash = hash_password(spec["password"]),
                    role          = spec["role"],
                    is_active     = True,
                ))
        db.commit()
    finally:
        db.close()

seed_dev_users()


app = FastAPI(title="Taqdeer API", version="1.0.0")

# Allow the frontend (any local origin) to call the API.
# Tighten allow_origins to the specific dev server URL in production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve uploaded files (avatars, answer-key DOCX, etc.) as static assets.
# URL pattern:  GET /uploads/avatars/{filename}
# Filesystem:   backend/uploads/avatars/{filename}
_uploads_dir = Path("uploads")
_uploads_dir.mkdir(exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(_uploads_dir)), name="uploads")

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(exams.router)
app.include_router(rubrics.router)
app.include_router(answer_files.router)
app.include_router(results.router)
app.include_router(admin.router)
app.include_router(activity.router)
app.include_router(notifications.router)
app.include_router(extraction.router)
app.include_router(grading.router)
app.include_router(gap_analysis.router)
app.include_router(reports.router)
app.include_router(reviewed_export.router)


@app.get("/")
def home():
    return {"message": "Taqdeer backend working"}
