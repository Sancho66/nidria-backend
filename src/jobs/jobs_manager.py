import asyncio
import uuid
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, sessionmaker

from shared.models.agent import Agent
from shared.models.job import JobConfig, JobRun
from src.core.enums import JobTriggeredBy
from src.core.exceptions import NotFoundError, ValidationError
from src.core.job_wrapper import run_job
from src.core.scheduler import JOB_REGISTRY, schedule_job
from src.jobs.jobs_repository import JobsRepository
from src.jobs.jobs_schema import JobConfigUpdateRequest


class JobsManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = JobsRepository(db)

    async def list_jobs(self) -> list[JobConfig]:
        return await self.repo.list_configs()

    async def _get_config(self, job_id: str) -> JobConfig:
        config = await self.repo.get_config(job_id)
        if config is None:
            raise NotFoundError("Job not found.")
        return config

    async def update_job(
        self,
        job_id: str,
        payload: JobConfigUpdateRequest,
        scheduler: BackgroundScheduler | None,
        session_local: sessionmaker[Session] | None,
    ) -> JobConfig:
        config = await self._get_config(job_id)
        data = payload.model_dump(exclude_unset=True)
        if "cron_expression" in data or "timezone" in data:
            try:
                CronTrigger.from_crontab(
                    data.get("cron_expression", config.cron_expression),
                    timezone=data.get("timezone", config.timezone),
                )
            except ValueError as exc:
                raise ValidationError(f"Invalid cron expression: {exc}") from exc
        for field, value in data.items():
            setattr(config, field, value)
        await self.db.commit()
        await self.db.refresh(config)
        # Hot-reload: reschedule in the RUNNING scheduler, no redeploy.
        if scheduler is not None and session_local is not None:
            schedule_job(scheduler, session_local, config)
        return config

    async def pause_job(self, job_id: str, until: datetime) -> JobConfig:
        config = await self._get_config(job_id)
        config.paused_until = until
        await self.db.commit()
        await self.db.refresh(config)
        return config

    async def resume_job(self, job_id: str) -> JobConfig:
        config = await self._get_config(job_id)
        config.paused_until = None
        await self.db.commit()
        await self.db.refresh(config)
        return config

    async def trigger_job(
        self,
        agent: Agent,
        job_id: str,
        dry_run: bool,
        session_local: sessionmaker[Session],
    ) -> JobRun:
        """Manual run, NOW, through the same wrapper as the scheduler —
        the dispatcher's SKIP LOCKED therefore also covers a manual
        trigger racing a tick."""
        await self._get_config(job_id)  # 404 before spawning the thread
        if job_id not in JOB_REGISTRY:
            raise ValidationError("Job has no registered pipeline.")

        def _run_sync() -> JobRun:
            with session_local() as db:
                return run_job(
                    db,
                    job_id=job_id,
                    pipeline=JOB_REGISTRY[job_id],
                    triggered_by=JobTriggeredBy.MANUAL,
                    triggered_by_agent_id=agent.id,
                    dry_run=dry_run,
                )

        return await asyncio.to_thread(_run_sync)

    async def list_runs(self, job_id: str, limit: int) -> list[JobRun]:
        config = await self._get_config(job_id)
        return await self.repo.list_runs(config.id, limit)

    async def get_run(self, job_id: str, run_id: uuid.UUID) -> JobRun:
        config = await self._get_config(job_id)
        run = await self.repo.get_run(config.id, run_id)
        if run is None:
            raise NotFoundError("Job run not found.")
        return run
