"""Job endpoints: start background work and poll its status."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.schemas import JobCreated, JobOut
from app.database.models import Job
from app.database.session import get_db
from app.orchestration.tasks import ping

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


@router.post("/ping", response_model=JobCreated, status_code=status.HTTP_202_ACCEPTED)
def start_ping(seconds: float = Query(3.0, ge=0, le=60), db: Session = Depends(get_db)) -> JobCreated:
    job = Job(kind="ping", status="queued", progress=0)
    db.add(job)
    db.commit()  # commit BEFORE enqueueing, so the worker always finds the row
    ping.apply_async(args=[seconds], task_id=str(job.id))
    return JobCreated(job_id=job.id)


@router.get("", response_model=list[JobOut])
def list_jobs(
    project_id: uuid.UUID | None = None,
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> list[Job]:
    query = select(Job).order_by(Job.created_at.desc()).limit(limit)
    if project_id is not None:
        query = query.where(Job.project_id == project_id)
    return list(db.scalars(query))


@router.get("/{job_id}", response_model=JobOut)
def get_job(job_id: uuid.UUID, db: Session = Depends(get_db)) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    return job
