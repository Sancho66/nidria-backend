"""Platform orchestration delegates all fictional writes to the isolated service."""

import uuid

from src.admin.demo_schema import (
    AgencyDemoBatchRequest,
    AgencyDemoBatchResponse,
    DemoBulkRequest,
    DemoBulkResponse,
    DemoCreateRequest,
    DemoExportResponse,
    DemoFilters,
    DemoJourneyInput,
    DemoJourneyPreview,
    DemoListResponse,
    DemoRead,
    DemoSelection,
    DemoTemplateRead,
)
from src.admin.demo_transport import DemoTransport


class AdminDemoManager:
    def __init__(self) -> None:
        self.transport = DemoTransport()

    async def create_agency_batch(
        self, actor_id: uuid.UUID, payload: AgencyDemoBatchRequest
    ) -> AgencyDemoBatchResponse:
        return AgencyDemoBatchResponse.model_validate(
            await self.transport.call(actor_id, "agency_batch", payload.model_dump(mode="json"))
        )

    async def list_demos(
        self,
        actor_id: uuid.UUID,
        page: int,
        page_size: int,
        search: str,
        filters: DemoFilters | None = None,
    ) -> DemoListResponse:
        result = await self.transport.call(
            actor_id,
            "list",
            {
                **(filters or DemoFilters()).model_dump(),
                "page": page,
                "page_size": page_size,
                "search": search,
            },
        )
        return DemoListResponse.model_validate(result)

    async def export_selection(
        self, actor_id: uuid.UUID, payload: DemoSelection
    ) -> DemoExportResponse:
        return DemoExportResponse.model_validate(
            await self.transport.call(actor_id, "export", payload.model_dump(mode="json"))
        )

    async def bulk(self, actor_id: uuid.UUID, payload: DemoBulkRequest) -> DemoBulkResponse:
        return DemoBulkResponse.model_validate(
            await self.transport.call(actor_id, "bulk", payload.model_dump(mode="json"))
        )

    async def archive(self, actor_id: uuid.UUID, batch: str) -> DemoExportResponse:
        return DemoExportResponse.model_validate(
            await self.transport.call(actor_id, "archive", {"batch": batch})
        )

    async def templates(self, actor_id: uuid.UUID) -> list[DemoTemplateRead]:
        result = await self.transport.call(actor_id, "templates", {})
        return [DemoTemplateRead.model_validate(item) for item in result]

    async def preview(self, actor_id: uuid.UUID, payload: DemoJourneyInput) -> DemoJourneyPreview:
        result = await self.transport.call(actor_id, "preview", payload.model_dump(mode="json"))
        return DemoJourneyPreview.model_validate(result)

    async def create(self, actor_id: uuid.UUID, payload: DemoCreateRequest) -> DemoRead:
        result = await self.transport.call(actor_id, "create", payload.model_dump(mode="json"))
        return DemoRead.model_validate(result)

    async def disable(self, actor_id: uuid.UUID, code: str) -> DemoRead:
        return DemoRead.model_validate(
            await self.transport.call(actor_id, "disable", {"code": code})
        )
