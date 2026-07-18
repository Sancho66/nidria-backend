"""APScheduler integration — Prism pattern: in-process BackgroundScheduler
(fly auto_stop_machines=off), SYNC sessions, a fresh Session per run.
Job crons live in `job_config` (data): edited at runtime via PATCH /jobs
with hot-reload, no redeploy."""

import contextlib
import logging
from functools import partial

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from shared.models.job import JobConfig
from src.core.config import get_settings
from src.core.enums import JobTriggeredBy
from src.core.job_wrapper import Pipeline, run_job
from src.digest.digest_job import run_notification_digest
from src.nurture.nurture_job import send_trial_nurture
from src.reminders.reminders_jobs import create_auto_reminders, dispatch_due_reminders

logger = logging.getLogger(__name__)

JOB_REGISTRY: dict[str, Pipeline] = {
    "dispatch_reminders": dispatch_due_reminders,
    "auto_reminders": create_auto_reminders,
    "trial_nurture": send_trial_nurture,
    "notification_digest": run_notification_digest,
}


def make_session_local() -> sessionmaker[Session]:
    engine = create_engine(get_settings().database_url_sync, pool_pre_ping=True)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _run_scheduled(session_local: sessionmaker[Session], job_id: str) -> None:
    with session_local() as db:
        run_job(
            db,
            job_id=job_id,
            pipeline=JOB_REGISTRY[job_id],
            triggered_by=JobTriggeredBy.SCHEDULER,
        )


def schedule_job(
    scheduler: BackgroundScheduler,
    session_local: sessionmaker[Session],
    config: JobConfig,
) -> bool:
    """(Re)schedule one config — also the hot-reload path of PATCH /jobs.
    Returns True when scheduled (enabled + known job_id)."""
    with contextlib.suppress(JobLookupError):
        scheduler.remove_job(config.job_id)
    if config.job_id not in JOB_REGISTRY:
        logger.warning("job_config %s has no registered pipeline, skipped", config.job_id)
        return False
    if not config.is_enabled:
        return False  # disabled → not programmed (boot or hot-reload)
    trigger = CronTrigger.from_crontab(config.cron_expression, timezone=config.timezone)
    scheduler.add_job(
        partial(_run_scheduled, session_local, config.job_id),
        trigger=trigger,
        id=config.job_id,
        name=config.name,
        max_instances=1,
        coalesce=True,
    )
    return True


def build_scheduler(session_local: sessionmaker[Session]) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="UTC")
    with session_local() as db:
        for config in db.execute(select(JobConfig)).scalars().all():
            schedule_job(scheduler, session_local, config)
    return scheduler
