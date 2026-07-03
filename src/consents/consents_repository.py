import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.consent import ConsentAcceptance


class ConsentsRepository:
    """Acceptance reads/inserts + agency-name lookups. The document
    queries (latest active per type, missing sets) live in
    core.rbac.consent_gate: single source shared with the enforcement."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_acceptance(
        self,
        actor_type: str,
        actor_id: uuid.UUID,
        document_type: str,
        document_version: int,
        agency_id: uuid.UUID | None,
    ) -> ConsentAcceptance | None:
        stmt = select(ConsentAcceptance).where(
            ConsentAcceptance.actor_type == actor_type,
            ConsentAcceptance.actor_id == actor_id,
            ConsentAcceptance.document_type == document_type,
            ConsentAcceptance.document_version == document_version,
            (
                ConsentAcceptance.agency_id.is_(None)
                if agency_id is None
                else ConsentAcceptance.agency_id == agency_id
            ),
        )
        return (await self.db.execute(stmt)).scalar_one_or_none()

    def add_acceptance(self, **kwargs: Any) -> ConsentAcceptance:
        row = ConsentAcceptance(**kwargs)
        self.db.add(row)
        return row

    async def agency_names(self, agency_ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
        if not agency_ids:
            return {}
        stmt = select(Agency.id, Agency.name).where(Agency.id.in_(agency_ids))
        return {agency_id: name for agency_id, name in (await self.db.execute(stmt)).all()}
