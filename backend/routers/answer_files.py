"""
CRUD router — /answer-files
Supports the Answer Files page (answer-files.html).

Upload normalisation
────────────────────
Both upload modes produce the exact same result: individual rows in `answer_files`.

  • Multiple PDF files  →  each file is saved directly, one DB row per file.
  • One ZIP file        →  PDFs are extracted in-memory (the ZIP is never stored),
                           then each extracted PDF follows the same path above.
  Non-PDF entries inside a ZIP are silently skipped.
"""

import io
import os
import zipfile
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from database import get_db
from deps import get_current_user
from models import AnswerFile, Exam, ExamModelAnswer, Rubric, User
from permissions import require_exam_access
from schemas import AnswerFileRead
from activity_log import log_activity

router = APIRouter(prefix="/answer-files", tags=["Answer Files"])

# Uploads are stored at  uploads/answers/{exam_id}/{filename}
# relative to the directory from which uvicorn is launched (the backend/ dir).
UPLOAD_DIR = Path("uploads/answers")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_exam_or_404(exam_id: int, db: Session) -> Exam:
    exam = db.get(Exam, exam_id)
    if exam is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Exam {exam_id} not found.",
        )
    return exam


def _get_answer_file_or_404(file_id: int, db: Session) -> AnswerFile:
    af = db.get(AnswerFile, file_id)
    if af is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Answer file {file_id} not found.",
        )
    return af


def _exam_upload_dir(exam_id: int) -> Path:
    """Return (and create if needed) the per-exam upload directory."""
    d = UPLOAD_DIR / str(exam_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_pdf_to_disk(data: bytes, original_name: str, exam_id: int) -> tuple[str, int, str]:
    """
    Write PDF bytes to disk.  If the filename already exists, a numeric suffix
    is appended to avoid collision.
    Returns (file_name, file_size, file_path).
    """
    dest_dir = _exam_upload_dir(exam_id)
    stem = Path(original_name).stem
    dest = dest_dir / original_name
    counter = 1
    while dest.exists():
        dest = dest_dir / f"{stem}_{counter}.pdf"
        counter += 1

    dest.write_bytes(data)
    return dest.name, len(data), str(dest)


# ── GET /answer-files/?exam_id=... ────────────────────────────────────────────

@router.get("/", response_model=List[AnswerFileRead], response_model_by_alias=True)
def list_answer_files(
    exam_id:      int,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Return all answer files for a specific exam, ordered by upload time.
    `exam_id` is required — cross-exam listing is not supported.
    """
    exam = _get_exam_or_404(exam_id, db)
    require_exam_access(exam, current_user)
    files = (
        db.query(AnswerFile)
        .filter(AnswerFile.exam_id == exam_id)
        .order_by(AnswerFile.uploaded_at.asc())
        .all()
    )
    return [AnswerFileRead.from_orm(f) for f in files]


# ── POST /answer-files/ ───────────────────────────────────────────────────────

@router.post("/", response_model=List[AnswerFileRead], response_model_by_alias=True,
             status_code=status.HTTP_201_CREATED)
async def upload_answer_files(
    exam_id:      int              = Form(...),
    files:        List[UploadFile] = File(...),
    current_user: User             = Depends(get_current_user),
    db:           Session          = Depends(get_db),
):
    """
    Upload answer PDFs for an exam.

    Accepted inputs
    ---------------
    • One or more .pdf files  →  stored directly, one row per file.
    • One .zip file           →  PDFs extracted in-memory, stored individually,
                                 one row per extracted PDF.  Non-PDF entries inside
                                 the ZIP are silently skipped.

    Business rules
    --------------
    1. Exam must exist.
    2. Exam must already have at least a rubric OR at least one model answer.
       (Both are not required — either one is sufficient to unlock uploads.)

    Uploading new files never modifies existing ExamResult rows — new students
    are created as ungraded and can be selectively graded from the grading page.
    """
    exam = _get_exam_or_404(exam_id, db)
    require_exam_access(exam, current_user)

    # Rule 2: require at least a rubric OR at least one model answer
    has_rubric     = db.query(Rubric).filter(Rubric.exam_id == exam_id).first() is not None
    has_answer_key = db.query(ExamModelAnswer).filter(ExamModelAnswer.exam_id == exam_id).first() is not None
    if not has_rubric and not has_answer_key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Upload blocked: this exam must have at least a rubric or an answer key "
                "before answer files can be uploaded."
            ),
        )

    # ── Normalise uploads → list of (original_name, bytes) ───────────────────

    pdf_entries: list[tuple[str, bytes]] = []

    for upload in files:
        raw = await upload.read()
        name_lower = (upload.filename or "").lower()

        if name_lower.endswith(".pdf"):
            pdf_entries.append((upload.filename, raw))

        elif name_lower.endswith(".zip"):
            try:
                zf = zipfile.ZipFile(io.BytesIO(raw))
            except zipfile.BadZipFile:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"'{upload.filename}' is not a valid ZIP archive.",
                )
            for entry in zf.infolist():
                # Skip directories and macOS/Windows metadata entries
                if entry.is_dir():
                    continue
                if Path(entry.filename).name.startswith((".", "__")):
                    continue
                if entry.filename.lower().endswith(".pdf"):
                    pdf_data = zf.read(entry.filename)
                    # Strip any sub-folder path; keep only the basename
                    base_name = Path(entry.filename).name
                    pdf_entries.append((base_name, pdf_data))
            zf.close()

        else:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail=(
                    f"'{upload.filename}' is not accepted. "
                    "Upload PDF files directly, or a single ZIP file containing PDFs."
                ),
            )

    if not pdf_entries:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No PDF files found in the uploaded content.",
        )

    # ── Persist each PDF and create one DB row per file ───────────────────────

    created: list[AnswerFile] = []
    for original_name, data in pdf_entries:
        file_name, file_size, file_path = _save_pdf_to_disk(data, original_name, exam_id)
        af = AnswerFile(
            exam_id   = exam_id,
            file_name = file_name,
            file_size = file_size,
            file_path = file_path,
        )
        db.add(af)
        created.append(af)

    # Flush to assign IDs and make rows visible within this transaction
    db.flush()

    # Sync count and advance workflow_step atomically with the inserts
    exam.answer_files_count = (
        db.query(AnswerFile).filter(AnswerFile.exam_id == exam_id).count()
    )
    if exam.workflow_step < 4:
        exam.workflow_step = 4

    log_activity(
        db, current_user.id, "upload",
        f"<strong>{len(created)} answer file{'s' if len(created) != 1 else ''}</strong> "
        f"uploaded for <strong>{exam.name}</strong>.",
    )

    db.commit()
    for af in created:
        db.refresh(af)

    return [AnswerFileRead.from_orm(af) for af in created]


# ── DELETE /answer-files/{id} ─────────────────────────────────────────────────

@router.delete("/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_answer_file(
    file_id:      int,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Delete a single answer file record and remove the stored file from disk.
    """
    af   = _get_answer_file_or_404(file_id, db)
    exam = _get_exam_or_404(af.exam_id, db)
    require_exam_access(exam, current_user)

    # Remove the physical file from disk (best-effort; don't block if missing)
    if af.file_path and os.path.exists(af.file_path):
        try:
            os.remove(af.file_path)
        except OSError:
            pass

    db.delete(af)
    db.flush()

    # Sync count atomically with the delete
    exam.answer_files_count = (
        db.query(AnswerFile).filter(AnswerFile.exam_id == exam.id).count()
    )
    db.commit()
