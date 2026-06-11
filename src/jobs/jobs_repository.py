import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.job import JobConfig, JobRun


class JobsRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_configs(self) -> list[JobConfig]:
        stmt = select(JobConfig).order_by(JobConfig.job_id)
        return list((await self.db.execute(stmt)).scalars())

    async def get_config(self, job_id: str) -> JobConfig | None:
        stmt = select(JobConfig).where(JobConfig.job_id == job_id)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def list_runs(self, job_config_id: uuid.UUID, limit: int) -> list[JobRun]:
        stmt = (
            select(JobRun)
            .where(JobRun.job_config_id == job_config_id)
            .order_by(JobRun.started_at.desc())
            .limit(limit)
        )
        return list((await self.db.execute(stmt)).scalars())

    async def get_run(self, job_config_id: uuid.UUID, run_id: uuid.UUID) -> JobRun | None:
        stmt = select(JobRun).where(JobRun.id == run_id, JobRun.job_config_id == job_config_id)
        return (await self.db.execute(stmt)).scalar_one_or_none()
