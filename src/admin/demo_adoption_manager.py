"""Combine anonymous demo references with authorized production counters."""

import re
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.demo_adoption_repository import DemoAdoptionRepository
from src.admin.demo_adoption_schema import (
    DemoAdoptionQuery,
    DemoAdoptionRead,
    DemoAdoptionResponse,
    DemoAdoptionSummary,
    DemoAdoptionTotals,
)
from src.admin.demo_schema import DemoListResponse
from src.demo_sandbox.measurement import demo_attribution


class DemoAdoptionManager:
    def __init__(self, db: AsyncSession) -> None:
        self.repo = DemoAdoptionRepository(db)

    async def read(self, query: DemoAdoptionQuery) -> DemoAdoptionResponse:
        source = await self.repo.page(query)
        return DemoAdoptionResponse(
            items=[
                DemoAdoptionRead(
                    **row,
                    accounts=1,
                    has_demo_reference=bool(
                        re.fullmatch(r"demo:[a-f0-9]{64}", row["reference"] or "")
                    ),
                )
                for row in source["rows"]
            ],
            total=source["totals"]["accounts"],
            totals=DemoAdoptionTotals(**source["totals"]),
            page=query.page,
            page_size=query.page_size,
            generated_at=datetime.now(UTC),
        )

    async def enrich(self, result: DemoListResponse) -> DemoListResponse:
        references = {item.code: demo_attribution(item.code) for item in result.items}
        counts = await self.repo.for_references(list(references.values()))
        for item in result.items:
            item.adoption = DemoAdoptionSummary(**counts.get(references[item.code], {}))
        return result
