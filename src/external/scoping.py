"""THE per-case scoping gate for external providers (wave B, RGPD core).

`get_case_for_external` is the SINGLE definition of "an external X may
access case D" — assigned, same agency, not soft-deleted. Every external
read/write resolves the case through it (documents, comments, portal),
so there is one place to audit. Returns None → the caller raises 404
(never 403: a non-assigned case's existence is not revealed). Re-queried
on every request (no cache) → unassigning cuts access immediately."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.case_external_assignment import CaseExternalAssignment
from shared.models.client_case import ClientCase


async def get_case_for_external(
    db: AsyncSession, external_agent: Agent, case_id: uuid.UUID
) -> ClientCase | None:
    stmt = (
        select(ClientCase)
        .join(CaseExternalAssignment, CaseExternalAssignment.case_id == ClientCase.id)
        .where(
            ClientCase.id == case_id,
            ClientCase.agency_id == external_agent.agency_id,
            ClientCase.deleted_at.is_(None),
            CaseExternalAssignment.agent_id == external_agent.id,
        )
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def list_assigned_cases(db: AsyncSession, external_agent: Agent) -> list[ClientCase]:
    stmt = (
        select(ClientCase)
        .join(CaseExternalAssignment, CaseExternalAssignment.case_id == ClientCase.id)
        .where(
            ClientCase.agency_id == external_agent.agency_id,
            ClientCase.deleted_at.is_(None),
            CaseExternalAssignment.agent_id == external_agent.id,
        )
        .order_by(ClientCase.created_at.desc(), ClientCase.id.desc())
    )
    return list((await db.execute(stmt)).scalars())
