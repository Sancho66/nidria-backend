"""A provider deposits on HIS steps only (02/10, src/external/scoping.py
`deposit_step_ids`).

Before, any provider assigned to a dossier could deposit — a free deposit, a
comment attachment, or a document requested from the client — on ANY step,
including the step of another provider. Now: only on the steps he is
responsible for (directly or through an external_contact that designates
him) or where he is a working participant (executant, provides_documents,
contributor); never where he is merely `informed`. The timeline says so per
step (`can_upload`), and both deposit endpoints refuse elsewhere with 403
`external.step_not_yours` — BEFORE any file is stored."""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.case_step_participant import CaseStepParticipant
from shared.models.document import Document
from shared.models.external_contact import ExternalContact
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

pytestmark = pytest.mark.usefixtures("rbac_baseline")

PDF = {"file": ("piece.pdf", b"%PDF-1.4 piece", "application/pdf")}
MINE, OTHER, PROVIDES, INFORMED, DESIGNATED = (
    "Mine",
    "Other provider",
    "Provides documents",
    "Informed",
    "Designated",
)
STEPS = (MINE, OTHER, PROVIDES, INFORMED, DESIGNATED)


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


@pytest_asyncio.fixture
async def external_role(db_session: AsyncSession) -> Role:
    return (
        await db_session.execute(
            select(Role).where(Role.is_external.is_(True), Role.name == "external_lawyer")
        )
    ).scalar_one()


@pytest_asyncio.fixture
async def lawyer(make_agent: MakeAgent, admin: Agent, external_role: Role) -> Agent:
    return await make_agent(
        agency_id=admin.agency_id, role=external_role, is_external=True, email="lawyer@pro.io"
    )


@pytest_asyncio.fixture
async def notary(make_agent: MakeAgent, admin: Agent, external_role: Role) -> Agent:
    return await make_agent(
        agency_id=admin.agency_id, role=external_role, is_external=True, email="notary@pro.io"
    )


async def _dossier(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    lawyer: Agent,
    notary: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
) -> tuple[uuid.UUID, dict[str, str], dict[str, str]]:
    """One dossier, five ACTIVE steps each asking the client for a document:
    the lawyer is responsible on MINE, the notary on OTHER, the lawyer is a
    provides_documents participant on PROVIDES, an informed participant on
    INFORMED, and responsible through a designating external_contact on
    DESIGNATED. Returns (case_id, {step: progress_id}, {step: requirement_id})."""
    ah = agent_headers(admin)
    tid = (await client.post("/journeys", headers=ah, json={"name": "T"})).json()["id"]
    for name in STEPS:
        sid = (await client.post(f"/journeys/{tid}/steps", headers=ah, json={"name": name})).json()[
            "id"
        ]
        r = await client.post(
            f"/journeys/{tid}/steps/{sid}/requirements",
            headers=ah,
            json={"kind": "document", "reference": f"Pièce {name}", "scope": "principal"},
        )
        assert r.status_code == 201, r.text
    principal = await make_expat_user(email=f"client-{uuid.uuid4().hex[:6]}@example.com")
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=principal.id, owner_agent_id=admin.id
    )
    steps = (
        await client.post(
            f"/cases/{case.id}/journey", headers=ah, json={"journey_template_id": tid}
        )
    ).json()
    pids = {s["name"]: s["id"] for s in steps}
    for provider in (lawyer, notary):
        r = await client.post(
            f"/cases/{case.id}/external-assignments",
            headers=ah,
            json={"agent_id": str(provider.id)},
        )
        assert r.status_code == 201, r.text

    async def responsible(step: str, body: dict[str, str]) -> None:
        r = await client.put(
            f"/cases/{case.id}/steps/{pids[step]}/responsible", headers=ah, json=body
        )
        assert r.status_code == 200, r.text

    await responsible(MINE, {"responsible_type": "agent", "responsible_agent_id": str(lawyer.id)})
    await responsible(OTHER, {"responsible_type": "agent", "responsible_agent_id": str(notary.id)})
    contact = ExternalContact(
        agency_id=admin.agency_id, case_id=case.id, name="Cabinet Lawyer", agent_id=lawyer.id
    )
    db_session.add(contact)
    for step, role in ((PROVIDES, "provides_documents"), (INFORMED, "informed")):
        db_session.add(
            CaseStepParticipant(
                case_step_progress_id=uuid.UUID(pids[step]),
                type="agent",
                agent_id=lawyer.id,
                role=role,
            )
        )
    await db_session.commit()
    await responsible(
        DESIGNATED, {"responsible_type": "external", "responsible_external_id": str(contact.id)}
    )
    for pid in pids.values():
        r = await client.patch(
            f"/cases/{case.id}/steps/{pid}", headers=ah, json={"status": "in_progress"}
        )
        assert r.status_code == 200, r.text

    detail = (await client.get(f"/external/cases/{case.id}", headers=agent_headers(lawyer))).json()
    rids = {s["name"]: s["requirements"][0]["id"] for s in detail["timeline"]}
    return case.id, pids, rids


async def _documents(db_session: AsyncSession) -> int:
    return int((await db_session.execute(select(func.count()).select_from(Document))).scalar_one())


async def test_timeline_says_where_the_provider_may_deposit(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    lawyer: Agent,
    notary: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
) -> None:
    case_id, _, _ = await _dossier(
        client,
        db_session,
        admin,
        lawyer,
        notary,
        make_client_case,
        make_expat_user,
        agent_headers,
    )
    for provider, expected in (
        (lawyer, {MINE: True, OTHER: False, PROVIDES: True, INFORMED: False, DESIGNATED: True}),
        (notary, {MINE: False, OTHER: True, PROVIDES: False, INFORMED: False, DESIGNATED: False}),
    ):
        detail = (
            await client.get(f"/external/cases/{case_id}", headers=agent_headers(provider))
        ).json()
        assert {s["name"]: s["can_upload"] for s in detail["timeline"]} == expected


async def test_requested_documents_only_on_his_steps(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    lawyer: Agent,
    notary: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
) -> None:
    case_id, _, rids = await _dossier(
        client,
        db_session,
        admin,
        lawyer,
        notary,
        make_client_case,
        make_expat_user,
        agent_headers,
    )
    h = agent_headers(lawyer)
    for step, allowed in (
        (OTHER, False),
        (INFORMED, False),
        (MINE, True),
        (PROVIDES, True),
        (DESIGNATED, True),
    ):
        before = await _documents(db_session)
        r = await client.post(
            f"/external/cases/{case_id}/requirements/{rids[step]}/document", headers=h, files=PDF
        )
        if allowed:
            assert r.status_code == 201, (step, r.text)
            assert await _documents(db_session) == before + 1
        else:
            assert r.status_code == 403, (step, r.text)
            assert r.json()["code"] == "external.step_not_yours"
            assert await _documents(db_session) == before  # nothing stored


async def test_free_deposit_only_on_his_steps(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    lawyer: Agent,
    notary: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
) -> None:
    """The free deposit is also the comment attachment's upload."""
    case_id, pids, _ = await _dossier(
        client,
        db_session,
        admin,
        lawyer,
        notary,
        make_client_case,
        make_expat_user,
        agent_headers,
    )
    h = agent_headers(lawyer)
    url = f"/external/cases/{case_id}/documents"

    before = await _documents(db_session)
    refused = await client.post(url, headers=h, files=PDF, data={"step_progress_id": pids[OTHER]})
    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "external.step_not_yours"
    off_step = await client.post(url, headers=h, files=PDF)
    assert off_step.status_code == 403, off_step.text
    assert off_step.json()["code"] == "external.step_not_yours"
    assert await _documents(db_session) == before

    # A step id foreign to the dossier keeps the upload core's answer.
    foreign = await client.post(
        url, headers=h, files=PDF, data={"step_progress_id": str(uuid.uuid4())}
    )
    assert foreign.status_code == 422, foreign.text
    assert foreign.json()["code"] == "progress.step_not_found"

    ok = await client.post(url, headers=h, files=PDF, data={"step_progress_id": pids[MINE]})
    assert ok.status_code == 201, ok.text
    # The other provider deposits on HIS step, not on the lawyer's.
    hn = agent_headers(notary)
    assert (
        await client.post(url, headers=hn, files=PDF, data={"step_progress_id": pids[OTHER]})
    ).status_code == 201
    assert (
        await client.post(url, headers=hn, files=PDF, data={"step_progress_id": pids[MINE]})
    ).status_code == 403
