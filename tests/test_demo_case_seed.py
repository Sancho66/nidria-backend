"""Example dossier seeded at agency activation (nurture bloc 2).

Covers: (a) wizard creation → demo case present, filled, timeline lived-in;
(b) THE critical one — the demo case moves NO usage signal (S0 stays S0);
(c) no email ever reaches the demo client (creation AND forgot-password);
(d) "voir comme le client" works on the never-logged demo expat;
(e) re-seeding is a no-op (marker); (f) the agency can delete the example
and NOTHING re-creates it (marker survives)."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.case_person import CasePerson
from shared.models.case_step_progress import CaseStepProgress
from shared.models.client_case import ClientCase
from shared.models.document import Document
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from shared.models.step_comment import StepComment
from shared.models.usage import UsageEvent
from src.agencies.demo_case_seed import DEMO_SEED_MARKER, seed_demo_case
from src.core import email
from src.core.storage import mock_store
from src.usage.usage_manager import UsageManager
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent

pytestmark = pytest.mark.usefixtures("rbac_baseline", "sector_templates")


async def _create_agency(
    client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
    *,
    slug: str = "demo-agency",
) -> dict:
    superadmin = await make_agent(role=system_roles["superadmin"])
    created = await client.post(
        "/agencies",
        headers=agent_headers(superadmin),
        json={
            "name": "Demo Agency",
            "slug": slug,
            "admin_email": f"admin@{slug}.example.com",
            "admin_first_name": "Ana",
            "admin_last_name": "Boss",
            "sectors": ["immigration"],  # mandatory at superadmin creation
        },
    )
    assert created.status_code == 201, created.text
    return created.json()


async def _demo_case(db: AsyncSession, agency_id: uuid.UUID) -> ClientCase | None:
    stmt = select(ClientCase).where(ClientCase.agency_id == agency_id, ClientCase.is_demo.is_(True))
    return (await db.execute(stmt)).scalar_one_or_none()


async def _admin_of(db: AsyncSession, agency_id: uuid.UUID) -> Agent:
    stmt = select(Agent).where(Agent.agency_id == agency_id)
    return (await db.execute(stmt)).scalar_one()


# --- (a) present, filled, lived-in ---------------------------------------------------------


async def test_agency_creation_seeds_a_filled_demo_case(
    client: AsyncClient,
    db_session: AsyncSession,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    body = await _create_agency(client, make_agent, system_roles, agent_headers)
    agency_id = uuid.UUID(body["agency"]["id"])

    case = await _demo_case(db_session, agency_id)
    assert case is not None
    assert case.journey_template_id is not None
    assert case.owner_agent_id is not None
    assert case.origin_country == "FR" and case.dest_country == "PT"

    # Simulated activation: the "account active" badge is TRUE.
    expat = await db_session.get(ExpatUser, case.principal_expat_user_id)
    assert expat is not None
    assert expat.email == "demo+demo-agency@nidria.app"
    assert expat.activated_at is not None

    # Lived-in timeline on the cloned immigration journey (6 steps): 2 DONE
    # (dated), 1 IN_PROGRESS, the rest TODO.
    progresses = list(
        (
            await db_session.execute(
                select(CaseStepProgress).where(CaseStepProgress.case_id == case.id)
            )
        ).scalars()
    )
    assert len(progresses) == 6
    statuses = sorted(p.status for p in progresses)
    assert statuses == ["done", "done", "in_progress", "todo", "todo", "todo"]
    assert all(p.completed_at is not None for p in progresses if p.status == "done")

    # Filled info page, one document (really stored), one client message.
    person = (
        await db_session.execute(select(CasePerson).where(CasePerson.case_id == case.id))
    ).scalar_one()
    assert person.nationality is not None and person.profession is not None
    document = (
        await db_session.execute(select(Document).where(Document.case_id == case.id))
    ).scalar_one()
    assert document.storage_path in mock_store
    comment = (
        await db_session.execute(
            select(StepComment).where(
                StepComment.case_step_progress_id.in_([p.id for p in progresses])
            )
        )
    ).scalar_one()
    assert comment.author_type == "expat"

    # And the agency actually SEES it through the API.
    admin = await _admin_of(db_session, agency_id)
    detail = await client.get(f"/cases/{case.id}", headers=agent_headers(admin))
    assert detail.status_code == 200, detail.text

    # The marker survives for idempotence.
    agency = await db_session.get(Agency, agency_id)
    assert agency is not None and agency.settings.get(DEMO_SEED_MARKER)


# --- (b) THE critical one: S0 stays S0 ------------------------------------------------------


async def test_demo_case_moves_no_usage_signal(
    client: AsyncClient,
    db_session: AsyncSession,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    body = await _create_agency(client, make_agent, system_roles, agent_headers, slug="s-zero")
    agency_id = uuid.UUID(body["agency"]["id"])

    # The ONLY event is the wizard's agency.activated — the demo case,
    # its journey, document and comment emitted NOTHING.
    events = list(
        (
            await db_session.execute(
                select(UsageEvent.event_type).where(UsageEvent.agency_id == agency_id)
            )
        ).scalars()
    )
    assert events == ["agency.activated"]

    usage = UsageManager(db_session)
    milestones = await usage.milestones(agency_id)
    assert set(milestones) == {"agence_activee"}
    assert await usage.compute_usage_state(agency_id) == "S0"

    counters = await usage.counters(agency_id)
    assert counters["nb_dossiers"] == 0
    assert counters["nb_dossiers_avec_client_actif"] == 0
    # Eric's call: the example JOURNEY is a normal reusable gift — only
    # the CASE is demo. It appears in the live template count (documented),
    # while premier_parcours_cree stays unset (no journey.created emitted).
    assert counters["nb_parcours"] == 1


# --- (c) no email ever reaches the demo client ----------------------------------------------


async def test_no_email_to_the_demo_client(
    client: AsyncClient,
    db_session: AsyncSession,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    await _create_agency(client, make_agent, system_roles, agent_headers, slug="no-mail")
    demo_address = "demo+no-mail@nidria.app"
    assert all(sent.to != demo_address for sent in email.outbox)

    # Even an explicit forgot-password answers 200 and sends NOTHING —
    # the sink itself suppresses demo recipients.
    before = len(email.outbox)
    resp = await client.post("/auth/expat/forgot-password", json={"email": demo_address})
    assert resp.status_code == 200
    assert len(email.outbox) == before


# --- (d) impersonation works on the demo expat ----------------------------------------------


async def test_impersonation_works_on_the_demo_client(
    client: AsyncClient,
    db_session: AsyncSession,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    body = await _create_agency(client, make_agent, system_roles, agent_headers, slug="see-it")
    agency_id = uuid.UUID(body["agency"]["id"])
    case = await _demo_case(db_session, agency_id)
    assert case is not None
    admin = await _admin_of(db_session, agency_id)

    issued = await client.post(
        f"/expat-users/{case.principal_expat_user_id}/impersonate",
        headers=agent_headers(admin),
    )
    assert issued.status_code == 200, issued.text
    token = issued.json()["access_token"]

    seen = await client.get("/expat/cases", headers={"Authorization": f"Bearer {token}"})
    assert seen.status_code == 200, seen.text
    assert [c["id"] for c in seen.json()] == [str(case.id)]


# --- (e) + (f) idempotence and deletion without resurrection ---------------------------------


async def test_reseed_is_noop_and_deletion_is_final(
    client: AsyncClient,
    db_session: AsyncSession,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    body = await _create_agency(client, make_agent, system_roles, agent_headers, slug="one-shot")
    agency_id = uuid.UUID(body["agency"]["id"])
    agency = await db_session.get(Agency, agency_id)
    assert agency is not None
    admin = await _admin_of(db_session, agency_id)

    # (e) a second seed (boot, script, whatever) is a strict no-op.
    assert await seed_demo_case(db_session, agency, admin) is None
    demo_cases = list(
        (
            await db_session.execute(
                select(ClientCase).where(
                    ClientCase.agency_id == agency_id, ClientCase.is_demo.is_(True)
                )
            )
        ).scalars()
    )
    assert len(demo_cases) == 1

    # (f) the agency deletes its example: a normal case, and NOTHING
    # re-creates it afterwards (the marker survives the deletion).
    deleted = await client.post(
        "/cases/bulk-delete",
        headers=agent_headers(admin),
        json={"case_ids": [str(demo_cases[0].id)]},
    )
    assert deleted.status_code == 200, deleted.text
    assert await seed_demo_case(db_session, agency, admin) is None
    db_session.expire_all()
    live = list(
        (
            await db_session.execute(
                select(ClientCase).where(
                    ClientCase.agency_id == agency_id,
                    ClientCase.is_demo.is_(True),
                    ClientCase.deleted_at.is_(None),
                )
            )
        ).scalars()
    )
    assert live == []
