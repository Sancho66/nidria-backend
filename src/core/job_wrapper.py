"""Sync wrapper running a pipeline within the JobRun lifecycle — ported
from Prism's engine/shared/job_wrapper.py, simplified: no root-logger
handler (it captured `logger.*` calls of third-party pipelines; our two
jobs log through the explicit callback).

- enabled / paused / starts_at–ends_at window checked first → SKIPPED run.
- The RUNNING row is committed upfront so operators can watch progress.
  KNOWN LIMITATION (MVP, V1.5 sweep): a process crash mid-run leaves an
  orphan RUNNING row.
- `log()` appends a timestamped line to JobRun.log_output and commits —
  progressive, readable mid-run.
"""

import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models.job import JobConfig, JobRun
from src.core.enums import JobRunStatus, JobTriggeredBy

logger = logging.getLogger(__name__)

LogFn = Callable[[str], None]
Pipeline = Callable[..., dict[str, Any]]


def run_job(
    db: Session,
    *,
    job_id: str,
    pipeline: Pipeline,
    triggered_by: JobTriggeredBy,
    triggered_by_agent_id: uuid.UUID | None = None,
    dry_run: bool = False,
) -> JobRun:
    config = db.execute(select(JobConfig).where(JobConfig.job_id == job_id)).scalar_one_or_none()
    if config is None:
        raise LookupError(f"Unknown job: {job_id}")

    now = datetime.now(UTC)
    skip_reason: str | None = None
    if not config.is_enabled:
        skip_reason = "job is disabled"
    elif config.paused_until is not None and config.paused_until > now:
        skip_reason = f"paused until {config.paused_until.isoformat()}"
    elif config.starts_at is not None and now < config.starts_at:
        skip_reason = "before starts_at window"
    elif config.ends_at is not None and now > config.ends_at:
        skip_reason = "after ends_at window"

    run = JobRun(
        job_config_id=config.id,
        job_id=job_id,
        status=JobRunStatus.RUNNING.value,
        triggered_by=triggered_by.value,
        triggered_by_agent_id=triggered_by_agent_id,
        log_output="",
    )

    if skip_reason is not None:
        run.status = JobRunStatus.SKIPPED.value
        run.finished_at = now
        run.duration_seconds = 0
        run.log_output = f"skipped: {skip_reason}\n"
        db.add(run)
        config.last_run_at = now
        config.last_run_status = run.status
        db.commit()
        return run

    db.add(run)
    db.commit()
    db.refresh(run)

    def log(message: str) -> None:
        timestamp = datetime.now(UTC).strftime("%H:%M:%S")
        run.log_output += f"[{timestamp}] {message}\n"
        db.commit()

    started = time.monotonic()
    try:
        stats = pipeline(db, log=log, dry_run=dry_run)
        run.status = JobRunStatus.SUCCESS.value
        run.stats = stats or {}
    except Exception as exc:  # noqa: BLE001 — job boundary, the error goes in the run
        db.rollback()  # discard the pipeline's uncommitted work
        run.status = JobRunStatus.FAILED.value
        run.error = f"{type(exc).__name__}: {exc}"
        logger.exception("Job %s failed", job_id)

    run.finished_at = datetime.now(UTC)
    run.duration_seconds = int(time.monotonic() - started)
    config.last_run_at = run.finished_at
    config.last_run_status = run.status
    db.commit()
    db.refresh(run)
    return run
