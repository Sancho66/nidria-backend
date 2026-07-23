"""Consent gate computations (point 16): who is missing which document.

Model-only queries (no domain import) so the central enforcement can call
them without layering violations; the consents Manager reuses the same
functions for the /consents/pending endpoints, so the gate and the screen
can never disagree.

Scope of the requirement:
- AGENT face: only the AGENCY ADMIN is gated (the system 'admin' role or
  its copy-on-write agency clone). One acceptance per agent, it binds the
  agency (spec Eric: "l'admin de l'agence"; other agents pass free at the
  MVP).
- EXPAT face: gated PER AGENCY where the client has at least one live
  case (the agency is the data controller; a client at two agencies
  accepts for each).
- EXTERNAL face: a provider (an is_external Agent) is gated for THE
  agency their account belongs to (a provider Agent has exactly one
  agency; get_case_for_external confirms their access never crosses it).
  A person working for two agencies has two provider accounts, one gate
  and one trace each. The pair shape mirrors the expat gate, so it stays
  correct if a provider ever spans agencies.

"Required" always means: the latest ACTIVE version of each type of the
audience's set. Publishing a new version therefore re-gates everyone
concerned with zero extra machinery."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.client_case import ClientCase
from shared.models.consent import ConsentAcceptance, ConsentDocument
from shared.models.rbac import Role
from src.core.enums import (
    AGENT_CONSENT_TYPES,
    EXPAT_CONSENT_TYPES,
    EXTERNAL_CONSENT_TYPES,
    ActorType,
)
from src.core.rbac.admin_roles import is_admin_role_clause


async def active_documents_by_type(
    db: AsyncSession, types: frozenset[str]
) -> dict[str, ConsentDocument]:
    """The latest ACTIVE version per type (highest version wins if a
    script left several actives)."""
    stmt = select(ConsentDocument).where(
        ConsentDocument.type.in_(types), ConsentDocument.is_active.is_(True)
    )
    latest: dict[str, ConsentDocument] = {}
    for doc in (await db.execute(stmt)).scalars():
        current = latest.get(doc.type)
        if current is None or doc.version > current.version:
            latest[doc.type] = doc
    return latest


async def _accepted_keys(
    db: AsyncSession, actor_type: ActorType, actor_id: uuid.UUID
) -> set[tuple[str, int, uuid.UUID | None]]:
    stmt = select(
        ConsentAcceptance.document_type,
        ConsentAcceptance.document_version,
        ConsentAcceptance.agency_id,
    ).where(
        ConsentAcceptance.actor_type == actor_type.value,
        ConsentAcceptance.actor_id == actor_id,
    )
    return {(t, v, a) for t, v, a in (await db.execute(stmt)).all()}


async def is_agency_admin(db: AsyncSession, agent: Agent) -> bool:
    """The agency admin: holder of the SYSTEM 'admin' role, or of its
    copy-on-write clone (an agency that edited the admin role rebinds its
    agents onto the clone; they are still 'the admin'). Consumes the single
    `is_admin_role_clause` definition shared with impersonation — the gate
    and the agency switcher can never disagree about who the admin is."""
    stmt = select(Role.id).where(Role.id == agent.role_id, is_admin_role_clause()).limit(1)
    return (await db.execute(stmt)).first() is not None


async def missing_for_agent(db: AsyncSession, agent: Agent) -> list[ConsentDocument]:
    """Active agency documents the agent has not accepted; [] for any
    agent that is not the agency admin (not gated at the MVP)."""
    if not await is_agency_admin(db, agent):
        return []
    required = await active_documents_by_type(db, AGENT_CONSENT_TYPES)
    if not required:
        return []
    accepted = await _accepted_keys(db, ActorType.AGENT, agent.id)
    accepted_versions = {(t, v) for t, v, _ in accepted}
    return [
        doc
        for doc in sorted(required.values(), key=lambda d: d.type)
        if (doc.type, doc.version) not in accepted_versions
    ]


async def expat_agency_ids(db: AsyncSession, expat_id: uuid.UUID) -> list[uuid.UUID]:
    """Agencies where the expat has at least one live (non-deleted) case."""
    stmt = (
        select(ClientCase.agency_id)
        .where(
            ClientCase.principal_expat_user_id == expat_id,
            ClientCase.deleted_at.is_(None),
        )
        .distinct()
    )
    return list((await db.execute(stmt)).scalars())


async def missing_for_expat(
    db: AsyncSession, expat_id: uuid.UUID
) -> list[tuple[uuid.UUID, ConsentDocument]]:
    """(agency_id, document) pairs still to accept, across every agency
    holding a live case of this client."""
    agency_ids = await expat_agency_ids(db, expat_id)
    if not agency_ids:
        return []
    required = await active_documents_by_type(db, EXPAT_CONSENT_TYPES)
    if not required:
        return []
    accepted = await _accepted_keys(db, ActorType.EXPAT, expat_id)
    return [
        (agency_id, doc)
        for agency_id in sorted(agency_ids)
        for doc in sorted(required.values(), key=lambda d: d.type)
        if (doc.type, doc.version, agency_id) not in accepted
    ]


def external_agency_ids(agent: Agent) -> list[uuid.UUID]:
    """The agencies a provider consents for. A provider Agent belongs to
    exactly one agency and its access never crosses it, so this is that
    single agency (a list, mirroring the expat scoping shape)."""
    return [agent.agency_id]


async def missing_for_external(
    db: AsyncSession, external_agent: Agent
) -> list[tuple[uuid.UUID, ConsentDocument]]:
    """(agency_id, document) pairs a provider still has to accept. Empty
    for a non-external agent (the gate never fires on the internal
    face)."""
    if not external_agent.is_external:
        return []
    required = await active_documents_by_type(db, EXTERNAL_CONSENT_TYPES)
    if not required:
        return []
    accepted = await _accepted_keys(db, ActorType.EXTERNAL, external_agent.id)
    return [
        (agency_id, doc)
        for agency_id in sorted(external_agency_ids(external_agent))
        for doc in sorted(required.values(), key=lambda d: d.type)
        if (doc.type, doc.version, agency_id) not in accepted
    ]
