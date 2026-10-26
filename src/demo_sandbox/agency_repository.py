"""Resolve one full fictional agency and its exact set of fixture identities."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.case_person import CasePerson
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser


@dataclass(frozen=True)
class AgencyDemoIdentity:
    agency: Agency
    agent: Agent
    cases: tuple[ClientCase, ...]
    metadata: dict[str, Any]


class AgencyDemoRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def resolve(
        self, code: str, *, allow_disabled: bool = False
    ) -> AgencyDemoIdentity | None:
        agency = await self.db.scalar(select(Agency).where(Agency.slug == f"demo-sandbox-{code}"))
        if agency is None or not isinstance(agency.settings.get("sandbox_full_demo"), dict):
            return None
        metadata = agency.settings["sandbox_full_demo"]
        try:
            expires = datetime.fromisoformat(metadata["expires_at"])
            agent_id = uuid.UUID(metadata["agent_id"])
            expected = {uuid.UUID(value) for value in metadata["case_ids"]}
        except (KeyError, ValueError, TypeError):
            return None
        if expires.tzinfo is None or not 8 <= len(expected) <= 12:
            return None
        if not allow_disabled and (
            agency.settings.get("sandbox_enabled") is not True or expires <= datetime.now(UTC)
        ):
            return None
        agent = await self.db.get(Agent, agent_id)
        cases = tuple(
            await self.db.scalars(select(ClientCase).where(ClientCase.agency_id == agency.id))
        )
        if (
            agent is None
            or agent.agency_id != agency.id
            or agent.is_external
            or agent.deactivated_at is not None
            or {case.id for case in cases} != expected
            or any(not case.is_demo or case.deleted_at is not None for case in cases)
        ):
            return None
        principals = {case.principal_expat_user_id for case in cases}
        clients = list(await self.db.scalars(select(ExpatUser).where(ExpatUser.id.in_(principals))))
        if (
            len(principals) != len(cases)
            or len(clients) != len(cases)
            or any(
                client.activated_at is None or client.preferred_lang != agency.default_language
                for client in clients
            )
        ):
            return None
        foreign = await self.db.scalar(
            select(ClientCase.id)
            .where(
                ClientCase.agency_id != agency.id,
                ClientCase.principal_expat_user_id.in_(principals),
            )
            .limit(1)
        )
        foreign_person = await self.db.scalar(
            select(CasePerson.id)
            .where(
                CasePerson.expat_user_id.in_(principals),
                ~CasePerson.case_id.in_(expected),
            )
            .limit(1)
        )
        if foreign is not None or foreign_person is not None:
            return None
        return AgencyDemoIdentity(agency, agent, cases, metadata)

    async def expat(self, identity: AgencyDemoIdentity, case_id: uuid.UUID) -> ExpatUser | None:
        case = next((case for case in identity.cases if case.id == case_id), None)
        return await self.db.get(ExpatUser, case.principal_expat_user_id) if case else None
