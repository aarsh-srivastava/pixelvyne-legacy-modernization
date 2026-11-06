"""Write job status to app-db from inside Celery tasks."""

import uuid
from typing import Any

from app.database.models import Job
from app.database.session import SessionLocal


def update_job(job_id: str | uuid.UUID, **fields: Any) -> None:
    """Set fields on a job row in its own short transaction. Unknown job ids are ignored."""
    with SessionLocal() as db:
        job = db.get(Job, uuid.UUID(str(job_id)))
        if job is None:
            return
        for key, value in fields.items():
            setattr(job, key, value)
        db.commit()
