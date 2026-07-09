"""THE per-case scoping gate for external providers (wave B, RGPD core).

`get_case_for_external` is the SINGLE definition of "an external X may
access case D" — same agency, not soft-deleted, and reachable by ONE of
two paths:
  (1) an explicit CaseExternalAssignment (the provider is an invited
      is_external Agent assigned to the case), OR
  (2) DESIGNATION: a directory `external_contact` that DESIGNATES this
      Agent (external_contact.agent_id == X) is responsible on a step of
      the case. This is how a notary named-then-invited acquires, on
      invitation, the history of every step its contact already carried —
      without re-pointing any step.

Every external read/write resolves the case through it, so there is one
place to audit. Returns None → the caller raises 404 (never 403: a
non-visible case's existence is not revealed). Re-queried every request.
"""

import uuid

from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.case_external_assignment import CaseExternalAssignment
from shared.models.case_step_progress import CaseStepProgress
from shared.models.client_case import ClientCase
from shared.models.external_contact import ExternalContact


def _visible(external_agent: Agent) -> ColumnElement[bool]:
    """Path (1) assignment OR path (2) designated-contact responsible."""
    assigned = (
        select(1)
        .select_from(CaseExternalAssignment)
        .where(
            CaseExternalAssignment.case_id == ClientCase.id,
            CaseExternalAssignment.agent_id == external_agent.id,
        )
        .exists()
    )
    designated = (
        select(1)
        .select_from(CaseStepProgress)
        .join(ExternalContact, ExternalContact.id == CaseStepProgress.responsible_external_id)
        .where(
            CaseStepProgress.case_id == ClientCase.id,
            ExternalContact.agent_id == external_agent.id,
        )
        .exists()
    )
    return or_(assigned, designated)


async def get_case_for_external(
    db: AsyncSession, external_agent: Agent, case_id: uuid.UUID
) -> ClientCase | None:
    stmt = select(ClientCase).where(
        ClientCase.id == case_id,
        ClientCase.agency_id == external_agent.agency_id,
        ClientCase.deleted_at.is_(None),
        _visible(external_agent),
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def list_assigned_cases(db: AsyncSession, external_agent: Agent) -> list[ClientCase]:
    stmt = (
        select(ClientCase)
        .where(
            ClientCase.agency_id == external_agent.agency_id,
            ClientCase.deleted_at.is_(None),
            _visible(external_agent),
        )
        .order_by(ClientCase.created_at.desc(), ClientCase.id.desc())
    )
    return list((await db.execute(stmt)).scalars())
