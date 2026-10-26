"""Durable, row-atomic fixture creation, serialized only inside the sandbox."""

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, aclosing, asynccontextmanager
from datetime import UTC, datetime

from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.demo_import_schema import (
    DemoImportExport,
    DemoImportIssue,
    DemoImportList,
    DemoImportRead,
    DemoImportRequest,
)
from src.admin.demo_schema import AgencyDemoBatchRequest, AgencyDemoInput
from src.core.exceptions import ConflictError
from src.demo_sandbox.agency_control import AgencyDemoControlManager
from src.demo_sandbox.catalog import DemoCatalog
from src.demo_sandbox.import_csv import export_csv, parse_import
from src.demo_sandbox.import_repository import DemoImportRepository


class DemoImportManager:
    def __init__(self, db: AsyncSession) -> None:
        self.repo = DemoImportRepository(db)

    async def start(self, actor_id: uuid.UUID, payload: DemoImportRequest) -> DemoImportRead:
        fingerprint = hashlib.sha256(
            json.dumps(
                payload.model_dump(mode="json", exclude={"request_id"}), sort_keys=True
            ).encode()
        ).hexdigest()
        existing = await self.repo.by_request(payload.request_id)
        if existing is not None:
            if existing["fingerprint"] != fingerprint:
                raise ConflictError(
                    "This request has already been used.", code="demo.request_conflict"
                )
            return await self.repo.summary(existing["id"])
        parsed = parse_import(payload.csv_text)
        if len(parsed) != payload.expected_count:
            raise ConflictError("Confirm the exact row count again.", code="demo.import_count")
        now, identifier = datetime.now(UTC), uuid.uuid4()
        await self.repo.insert(
            {
                "id": identifier,
                "request_id": payload.request_id,
                "actor_id": actor_id,
                "fingerprint": fingerprint,
                "batch": payload.batch,
                "lifetime_days": payload.lifetime_days,
                "status": "queued" if any(not row.errors for row in parsed) else "completed",
                "total": len(parsed),
                "created_at": now,
                "updated_at": now,
            },
            parsed,
        )
        return await self.repo.summary(identifier)

    async def recent(self) -> DemoImportList:
        return DemoImportList(items=await self.repo.recent())

    async def read(self, identifier: uuid.UUID) -> DemoImportRead:
        return await self.repo.summary(identifier)

    async def export_batch(self, batch: str) -> DemoImportExport:
        if await self.repo.batch_running(batch):
            raise ConflictError("The import is still running.", code="demo.import_running")
        output = await self.repo.batch_output(batch)
        if len(output) > 5000:
            raise ConflictError("The batch is too large to export.", code="demo.import_size")
        fields = ["email", "company_name", "demo_url", "lot", "expiration"]
        values = [
            [
                row["email"],
                row["name"],
                f"https://app.nidria.com/demo/{row['slug'].removeprefix('demo-sandbox-')}",
                batch,
                row["expires"] or "",
            ]
            for row in output
        ]
        return DemoImportExport(csv=export_csv(fields, values), count=len(values))

    async def export(self, identifier: uuid.UUID, kind: str) -> DemoImportExport:
        job = await self.repo.get(identifier)
        if job["status"] != "completed":
            raise ConflictError("The import is still running.", code="demo.import_running")
        output = await self.repo.output(identifier, "succeeded" if kind == "links" else "error")
        if kind == "links":
            fields = ["email", "company_name", "demo_url", "lot", "expiration"]
            values = [[str(row["result"].get(key) or "") for key in fields] for row in output]
        else:
            fields = ["row", "column", "reason"]
            values = [
                [str(issue[key]) for key in fields] for row in output for issue in row["errors"]
            ]
        return DemoImportExport(csv=export_csv(fields, values), count=len(values))


class RowCreationFailed(Exception):
    def __init__(self, job_id: uuid.UUID, number: int) -> None:
        self.job_id, self.number = job_id, number
        super().__init__("Fictional demo row creation failed")


@asynccontextmanager
async def import_transaction() -> AsyncIterator[AsyncSession]:
    from src.demo_sandbox.control import control_db

    # Explicit closure rolls back the OUTER transaction even when a reused
    # manager has committed savepoints before the caller fails or is cancelled.
    async with aclosing(control_db()) as stream:
        async for db in stream:
            yield db


async def import_tick(
    catalog: DemoCatalog,
    transaction: Callable[[], AbstractAsyncContextManager[AsyncSession]] = import_transaction,
) -> bool:
    try:
        async with transaction() as db:
            repo = DemoImportRepository(db)
            row = await repo.next_row()
            if row is None:
                return False
            try:
                manager = AgencyDemoControlManager(db, catalog)
                response = await manager.create_batch(
                    row["actor_id"],
                    AgencyDemoBatchRequest(
                        request_id=row["job_id"],
                        items=[AgencyDemoInput.model_validate(row["data"])],
                        batch=row["batch"],
                        lifetime_days=row["lifetime_days"],
                    ),
                )
                demo = response.items[0]
                await repo.finish_row(
                    row["job_id"],
                    row["row_number"],
                    {
                        "email": row["data"]["email"],
                        "company_name": demo.name,
                        "demo_url": demo.public_url,
                        "lot": demo.batch,
                        "expiration": demo.expires_at.isoformat() if demo.expires_at else "",
                    },
                    reused=demo.code in manager.reused_codes,
                )
            except (OperationalError, InterfaceError):
                # A lost connection is retried after reconnect/restart, never
                # converted into a permanent row error with a partial fixture.
                raise
            except Exception as exc:
                raise RowCreationFailed(row["job_id"], row["row_number"]) from exc
        return True
    except RowCreationFailed as failure:
        # The failed fixture and every nested business commit rolled back.
        # The next row can proceed; internal exceptions never enter the CSV.
        async with transaction() as db:
            await DemoImportRepository(db).finish_row(
                failure.job_id,
                failure.number,
                {},
                errors=[
                    DemoImportIssue(
                        row=failure.number,
                        column="creation",
                        reason="Could not create this demo. Retry this row in the same batch.",
                    )
                ],
            )
        logging.getLogger(__name__).warning(
            "Demo import row failed: job=%s row=%s", failure.job_id, failure.number
        )
        return True


async def import_loop(catalog: DemoCatalog) -> None:
    while True:
        try:
            progressed = await import_tick(catalog)
            await asyncio.sleep(0.1 if progressed else 2)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger(__name__).error(
                "Demo import worker unavailable; pending rows will resume"
            )
            await asyncio.sleep(10)
