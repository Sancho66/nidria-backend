"""Persistence for the isolated queue; no production connection is used."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.demo_import import jobs, metadata, rows
from src.admin.demo_import_schema import DemoImportIssue, DemoImportRead
from src.core.exceptions import NotFoundError
from src.demo_sandbox.import_csv import ImportRow


async def prepare_imports(db: AsyncSession) -> None:
    connection = await db.connection()
    await connection.run_sync(metadata.create_all)
    for table in ("demo_import_job", "demo_import_row"):
        await db.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
    await db.execute(
        text("""
        CREATE INDEX IF NOT EXISTS ix_demo_batch_email ON agency
        ((settings->>'sandbox_batch'), lower(settings->'sandbox_full_demo'->>'email'))
        WHERE slug LIKE 'demo-sandbox-%'
    """)
    )


class DemoImportRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def by_request(self, request_id: uuid.UUID) -> Any:
        await self.db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"demo-import:{request_id}"},
        )
        return (
            (await self.db.execute(select(jobs).where(jobs.c.request_id == request_id)))
            .mappings()
            .one_or_none()
        )

    async def insert(self, job: dict[str, Any], parsed: list[ImportRow]) -> None:
        await self.db.execute(insert(jobs).values(**job))
        values = [
            {
                "job_id": job["id"],
                "row_number": row.number,
                "data": row.data,
                "status": "error" if row.errors else "pending",
                "reused": False,
                "result": {},
                "errors": [error.model_dump() for error in row.errors],
            }
            for row in parsed
        ]
        for start in range(0, len(values), 250):
            await self.db.execute(insert(rows), values[start : start + 250])

    async def get(self, job_id: uuid.UUID) -> Any:
        result = (
            (await self.db.execute(select(jobs).where(jobs.c.id == job_id)))
            .mappings()
            .one_or_none()
        )
        if result is None:
            raise NotFoundError("Demo import not found.", code="demo.import_not_found")
        return result

    async def summary(self, job_id: uuid.UUID) -> DemoImportRead:
        job = await self.get(job_id)
        created, reused, failed = (
            await self.db.execute(
                select(
                    func.count().filter((rows.c.status == "succeeded") & ~rows.c.reused),
                    func.count().filter((rows.c.status == "succeeded") & rows.c.reused),
                    func.count().filter(rows.c.status == "error"),
                ).where(rows.c.job_id == job_id)
            )
        ).one()
        errors = await self.db.scalars(
            select(rows.c.errors)
            .where(rows.c.job_id == job_id, rows.c.status == "error")
            .order_by(rows.c.row_number)
            .limit(20)
        )
        return DemoImportRead(
            **{
                key: job[key]
                for key in (
                    "id",
                    "batch",
                    "lifetime_days",
                    "status",
                    "total",
                    "created_at",
                    "updated_at",
                )
            },
            created=created,
            reused=reused,
            failed=failed,
            processed=created + reused + failed,
            errors=[DemoImportIssue.model_validate(issue) for group in errors for issue in group][
                :20
            ],
        )

    async def recent(self) -> list[DemoImportRead]:
        identifiers = list(
            await self.db.scalars(select(jobs.c.id).order_by(jobs.c.created_at.desc()).limit(20))
        )
        return [await self.summary(identifier) for identifier in identifiers]

    async def next_row(self) -> Any:
        locked = await self.db.scalar(
            text(
                "SELECT pg_try_advisory_xact_lock(hashtextextended('nidria-demo-import-worker', 0))"
            )
        )
        if not locked:
            return None
        return (
            (
                await self.db.execute(
                    select(
                        rows.c.job_id,
                        rows.c.row_number,
                        rows.c.data,
                        jobs.c.actor_id,
                        jobs.c.batch,
                        jobs.c.lifetime_days,
                    )
                    .join(jobs, jobs.c.id == rows.c.job_id)
                    .where(rows.c.status == "pending", jobs.c.status.in_(["queued", "running"]))
                    .order_by(jobs.c.created_at, rows.c.row_number)
                    .limit(1)
                    .with_for_update(of=rows)
                )
            )
            .mappings()
            .one_or_none()
        )

    async def finish_row(
        self,
        job_id: uuid.UUID,
        number: int,
        result: dict[str, Any],
        reused: bool = False,
        errors: list[DemoImportIssue] | None = None,
    ) -> None:
        await self.db.execute(
            update(rows)
            .where(rows.c.job_id == job_id, rows.c.row_number == number, rows.c.status == "pending")
            .values(
                status="error" if errors else "succeeded",
                data={},
                result=result,
                reused=reused,
                errors=[issue.model_dump() for issue in (errors or [])],
            )
        )
        pending = await self.db.scalar(
            select(func.count())
            .select_from(rows)
            .where(rows.c.job_id == job_id, rows.c.status == "pending")
        )
        await self.db.execute(
            update(jobs)
            .where(jobs.c.id == job_id)
            .values(
                status="running" if pending else "completed",
                updated_at=datetime.now(UTC),
            )
        )

    async def output(self, job_id: uuid.UUID, status: str) -> list[Any]:
        return list(
            (
                await self.db.execute(
                    select(rows.c.result, rows.c.errors)
                    .where(rows.c.job_id == job_id, rows.c.status == status)
                    .order_by(rows.c.row_number)
                )
            ).mappings()
        )

    async def batch_output(self, batch: str) -> list[Any]:
        return list(
            (
                await self.db.execute(
                    select(
                        Agency.name,
                        Agency.slug,
                        Agency.settings["sandbox_full_demo"]["email"].astext.label("email"),
                        func.coalesce(
                            Agency.settings["sandbox_expires_at"].astext,
                            Agency.settings["sandbox_full_demo"]["expires_at"].astext,
                        ).label("expires"),
                    )
                    .where(
                        Agency.slug.startswith("demo-sandbox-"),
                        Agency.settings["sandbox_batch"].astext == batch,
                        Agency.settings["sandbox_full_demo"]["email"].astext.is_not(None),
                    )
                    .order_by(Agency.name, Agency.id)
                    .limit(5001)
                )
            ).mappings()
        )

    async def batch_running(self, batch: str) -> bool:
        return bool(
            await self.db.scalar(
                select(jobs.c.id)
                .where(jobs.c.batch == batch, jobs.c.status != "completed")
                .limit(1)
            )
        )
