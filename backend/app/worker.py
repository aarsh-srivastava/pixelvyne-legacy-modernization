"""Celery application.

Started by docker-compose as:
    celery -A app.worker:celery_app worker --loglevel=info --concurrency=2
"""

from celery import Celery

from app.config import settings

celery_app = Celery(
    "pixelvyne",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["app.orchestration.tasks"],
)

celery_app.conf.update(
    task_acks_late=True,            # a task killed mid-run is redelivered, not lost
    worker_prefetch_multiplier=1,   # long jobs: take one at a time
    task_time_limit=900,            # hard stop at 15 minutes
    task_soft_time_limit=840,
    task_track_started=True,
    result_expires=60 * 60 * 24,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Kolkata",
)
