"""Consume email_sync requests; cron and manual requests share the same durable queue."""

import logging
import os
import signal
from contextlib import suppress
from pathlib import Path
from threading import Event

from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from shared.models.job import JobRun
from src.core.enums import JobTriggeredBy
from src.core.job_wrapper import run_job
from src.core.scheduler import make_session_local
from src.email_campaigns.sync_job import JOB_ID, run_email_sync

logger = logging.getLogger(__name__)
LOCK_KEY = 5418091136301124


def consume_one(engine: Engine) -> bool:
    # Pin the lock and the job session to one physical connection even across
    # progress commits. A replacement worker can resume after a process exit.
    with engine.connect() as connection:
        locked = connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_KEY})
        connection.commit()
        if not locked:
            return False
        try:
            with Session(bind=connection, expire_on_commit=False) as db:
                run = db.scalar(
                    select(JobRun)
                    .where(
                        JobRun.job_id == JOB_ID,
                        JobRun.status.in_(("queued", "running")),
                        JobRun.stats["worker_dispatch"].as_boolean().is_(True),
                    )
                    .order_by(JobRun.started_at, JobRun.id)
                    .limit(1)
                )
                if run is None:
                    return False
                logger.info("Processing email_sync run %s on the dedicated worker", run.id)
                run_job(
                    db,
                    job_id=JOB_ID,
                    pipeline=run_email_sync,
                    queued_run=run,
                    triggered_by=JobTriggeredBy(run.triggered_by),
                    dry_run=bool(run.stats.get("dry_run")),
                )
                logger.info("Dedicated email_sync run %s finished: %s", run.id, run.status)
                return True
        finally:
            try:
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_KEY})
                connection.commit()
            except Exception:
                connection.invalidate()
                raise


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    if os.environ.get("FLY_PROCESS_GROUP") == "worker":
        # Let monitoring run even during a CPU or memory spike in the provider job.
        os.nice(5)
        with suppress(OSError):
            Path("/proc/self/oom_score_adj").write_text("500")
    stop = Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    engine: Engine = make_session_local(email_worker=True).kw["bind"]
    logger.info("Dedicated email_sync worker started")
    try:
        while not stop.is_set():
            try:
                if consume_one(engine):
                    continue
            except Exception as exc:  # noqa: BLE001
                logger.warning("Worker polling failed: %s", type(exc).__name__)
            stop.wait(5)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
