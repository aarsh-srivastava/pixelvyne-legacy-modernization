"""Celery tasks. Each task's Celery id equals its row id in the jobs table.

Pattern for every task:
    update_job(id, status="running", ...)  ->  work, reporting stage/progress
    ->  update_job(id, status="succeeded", result=...)   or   status="failed", error=...
"""

import socket
import time

from app.orchestration.job_tracking import update_job
from app.worker import celery_app


@celery_app.task(name="pixelvyne.ping", bind=True)
def ping(self, seconds: float = 3.0) -> dict:
    """Proves API -> Redis -> worker -> app-db -> API works end to end."""
    job_id = self.request.id
    update_job(job_id, status="running", stage="starting", progress=0)
    try:
        steps = 3
        for i in range(steps):
            update_job(job_id, stage=f"step {i + 1} of {steps}", progress=int(100 * i / steps))
            time.sleep(seconds / steps)
        result = {"pong": True, "worker": socket.gethostname(), "slept_seconds": seconds}
        update_job(job_id, status="succeeded", stage="done", progress=100, result=result)
        return result
    except Exception as exc:
        update_job(job_id, status="failed", error=f"{exc.__class__.__name__}: {exc}")
        raise
