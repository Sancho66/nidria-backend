"""APScheduler integration — Prism pattern: in-process BackgroundScheduler
(fly auto_stop_machines=off), SYNC sessions, a fresh Session per run.
Job crons live in `job_config` (data): edited at runtime via PATCH /jobs
with hot-reload, no redeploy.

The hot-reload reaches the scheduler of the process that served the PATCH
only. The scheduler's own job, `reconcile_jobs` every minute, brings it in
line with `job_config` whoever changed the table: the administration script
(scripts/email_admin.py, another process), another machine's PATCH."""

import contextlib
import logging
from functools import partial

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from shared.models.job import JobConfig
from src.admin.platform_task_digest_job import send_platform_task_watcher_digests
from src.agencies.agencies_jobs import expire_agent_invitations
from src.agencies.onboarding_email_job import send_onboarding_emails
from src.cases.activation_jobs import send_activation_reminders
from src.core.config import get_settings
from src.core.enums import JobTriggeredBy
from src.core.job_wrapper import Pipeline, run_job
from src.digest.digest_job import run_notification_digest
from src.document_templates.document_templates_jobs import sweep_document_template_drafts
from src.email_campaigns.sync_job import run_email_sync
from src.nurture.nurture_job import send_trial_nurture
from src.reminders.reminders_jobs import create_auto_reminders, dispatch_due_reminders

logger = logging.getLogger(__name__)

JOB_REGISTRY: dict[str, Pipeline] = {
    "dispatch_reminders": dispatch_due_reminders,
    "auto_reminders": create_auto_reminders,
    "trial_nurture": send_trial_nurture,
    "notification_digest": run_notification_digest,
    "platform_task_watcher_digest": send_platform_task_watcher_digests,
    "expire_agent_invitations": expire_agent_invitations,
    "onboarding_email": send_onboarding_emails,
    "activation_reminders": send_activation_reminders,
    "sweep_document_template_drafts": sweep_document_template_drafts,
    # Email module (spec 10.3): reads Lemlist only, seeded DISABLED.
    "email_sync": run_email_sync,
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


#: The scheduler's own job, bringing it in line with `job_config`. Its id is
#: no `job_config` id a pipeline can have (a colon, outside JOB_REGISTRY): the
#: reconciliation never touches it, and it never runs through `run_job`.
RECONCILE_JOB_ID = "scheduler:job_config"
RECONCILE_INTERVAL_SECONDS = 60


def reconcile_jobs(
    scheduler: BackgroundScheduler, session_local: sessionmaker[Session]
) -> dict[str, int]:
    """Bring the programmed jobs in line with `job_config`, read only. A job
    enabled there and known (JOB_REGISTRY) is programmed, or programmed again
    when its trigger changed (cron, timezone); a disabled one is removed; an
    unchanged one is left alone, its next run untouched; an unknown job_id
    is ignored. Returns the counts (logged when something changed)."""
    counts = dict.fromkeys(
        ("scheduled", "rescheduled", "removed", "unchanged", "unknown", "failed"), 0
    )
    with session_local() as db:
        configs = list(db.execute(select(JobConfig)).scalars().all())
    for config in configs:
        if config.job_id not in JOB_REGISTRY:
            counts["unknown"] += 1
            continue
        try:
            current = scheduler.get_job(config.job_id)
            if not config.is_enabled:
                if current is None:
                    counts["unchanged"] += 1
                else:
                    scheduler.remove_job(config.job_id)
                    counts["removed"] += 1
                continue
            trigger = CronTrigger.from_crontab(config.cron_expression, timezone=config.timezone)
            # repr: every field, the timezone, the bounds and the jitter.
            if current is not None and repr(current.trigger) == repr(trigger):
                counts["unchanged"] += 1
                continue
            schedule_job(scheduler, session_local, config)
            counts["scheduled" if current is None else "rescheduled"] += 1
        except Exception:  # noqa: BLE001 (one row never stops the others; counted)
            counts["failed"] += 1
    if counts["scheduled"] or counts["rescheduled"] or counts["removed"] or counts["failed"]:
        logger.info(
            "job_config reconciled: %d scheduled, %d rescheduled, %d removed, %d failed",
            counts["scheduled"],
            counts["rescheduled"],
            counts["removed"],
            counts["failed"],
        )
    return counts


def _reconcile_scheduled(
    scheduler: BackgroundScheduler, session_local: sessionmaker[Session]
) -> None:
    """The reconciliation as the scheduler runs it: a failure (the database
    out of reach) is logged by its class only, and the next minute tries
    again."""
    try:
        reconcile_jobs(scheduler, session_local)
    except Exception as exc:  # noqa: BLE001 (the job boundary: a class name, never a message)
        logger.warning("job_config reconciliation failed: %s", type(exc).__name__)


class _QuietReconciliation(logging.Filter):
    """APScheduler logs two INFO lines on every run of a job; for the
    reconciliation, one a minute, they say nothing: dropped. Its warnings
    and errors pass, and `reconcile_jobs` logs what it changed."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno > logging.INFO or not isinstance(record.args, tuple):
            return True
        return not any(getattr(arg, "id", None) == RECONCILE_JOB_ID for arg in record.args)


_QUIET_RECONCILIATION = _QuietReconciliation()


def build_scheduler(session_local: sessionmaker[Session]) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="UTC")
    with session_local() as db:
        for config in db.execute(select(JobConfig)).scalars().all():
            schedule_job(scheduler, session_local, config)
    # Idempotent: a logger keeps one instance of a filter.
    logging.getLogger("apscheduler.executors.default").addFilter(_QUIET_RECONCILIATION)
    scheduler.add_job(
        partial(_reconcile_scheduled, scheduler, session_local),
        trigger=IntervalTrigger(seconds=RECONCILE_INTERVAL_SECONDS),
        id=RECONCILE_JOB_ID,
        name="job_config reconciliation",
        max_instances=1,
        coalesce=True,
        # Late (a busy machine) is fine: run once, never a "missed" warning.
        misfire_grace_time=None,
    )
    return scheduler
