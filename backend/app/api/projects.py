"""Projects and legacy source upload."""

import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, undefer

from app.api.schemas import ProjectCreate, ProjectOut, SourceFileDetail, SourceFileOut
from app.config import settings
from app.database.models import Project, SourceFile
from app.database.session import get_db
from app.ingestion.upload import UploadError, extract_files

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])


def _project_or_404(db: Session, project_id: uuid.UUID) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")
    return project


def _with_count(db: Session, project: Project) -> ProjectOut:
    count = db.scalar(select(func.count()).where(SourceFile.project_id == project.id))
    out = ProjectOut.model_validate(project)
    out.file_count = count or 0
    return out


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_project(body: ProjectCreate, db: Session = Depends(get_db)) -> ProjectOut:
    project = Project(**body.model_dump())
    db.add(project)
    db.commit()
    db.refresh(project)
    return _with_count(db, project)


@router.get("", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db)) -> list[ProjectOut]:
    projects = db.scalars(select(Project).order_by(Project.created_at.desc())).all()
    return [_with_count(db, p) for p in projects]


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: uuid.UUID, db: Session = Depends(get_db)) -> ProjectOut:
    return _with_count(db, _project_or_404(db, project_id))


@router.post(
    "/{project_id}/files",
    response_model=list[SourceFileOut],
    status_code=status.HTTP_201_CREATED,
)
def upload_files(
    project_id: uuid.UUID,
    files: list[UploadFile] = File(..., description="SQL scripts or a ZIP of them"),
    db: Session = Depends(get_db),
) -> list[SourceFile]:
    """Upload legacy scripts. Re-uploading a path replaces that file's content."""
    project = _project_or_404(db, project_id)
    limit = settings.MAX_UPLOAD_MB * 1_048_576

    extracted = []
    for upload in files:
        data = upload.file.read(limit + 1)
        if len(data) > limit:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                f"{upload.filename} is larger than {settings.MAX_UPLOAD_MB} MB",
            )
        try:
            extracted.extend(extract_files(upload.filename or "", data, limit))
        except UploadError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    existing = {
        f.path: f
        for f in db.scalars(select(SourceFile).where(SourceFile.project_id == project.id))
    }
    saved: list[SourceFile] = []
    for item in extracted:
        row = existing.get(item.path)
        if row is None:
            row = SourceFile(project_id=project.id, path=item.path)
            db.add(row)
            existing[item.path] = row
        row.content = item.content
        row.size_bytes = item.size_bytes
        row.sha256 = item.sha256
        saved.append(row)

    project.status = "uploaded"
    db.commit()
    for row in saved:
        db.refresh(row)
    return saved


@router.get("/{project_id}/files", response_model=list[SourceFileOut])
def list_files(project_id: uuid.UUID, db: Session = Depends(get_db)) -> list[SourceFile]:
    _project_or_404(db, project_id)
    return list(
        db.scalars(
            select(SourceFile).where(SourceFile.project_id == project_id).order_by(SourceFile.path)
        )
    )


@router.get("/{project_id}/files/{file_id}", response_model=SourceFileDetail)
def get_file(
    project_id: uuid.UUID, file_id: uuid.UUID, db: Session = Depends(get_db)
) -> SourceFileDetail:
    # content is deferred on the model; load it explicitly and serialise while the
    # session is still open.
    row = db.get(SourceFile, file_id, options=[undefer(SourceFile.content)])
    if row is None or row.project_id != project_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found")
    return SourceFileDetail.model_validate(row)
