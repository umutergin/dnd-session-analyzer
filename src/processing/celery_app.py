from celery import Celery

from src.config import settings

# Create Celery app
celery_app = Celery(
    "dnd_recorder",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["src.processing.tasks"],
)

# Configure Celery
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=14400,  # 4 hour max per task (supports 4-hour sessions)
    task_soft_time_limit=13800,  # 3h 50min soft limit
    worker_prefetch_multiplier=1,  # Process one task at a time
    task_acks_late=True,  # Ack after task completes
    task_reject_on_worker_lost=True,
)
