"""Point 16 battery: blocking clickwrap consent.

The gate is STRUCTURAL (enforcement, like the point-12 read-only mask):
outside CONSENT_EXEMPT, an agency admin without the two agency documents
accepted is 403 consent.required everywhere; an expat is gated PER AGENCY
holding a live case. Publishing a new version re-gates by construction
(the gate compares to the latest active version). The docs are NOT seeded
by the test harness: every other battery runs ungated; this one seeds them
explicitly."""

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.consent import ConsentAcceptance, ConsentDocument
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from src.consents.consents_seed import content_sha256, seed_consent_documents
from src.consents.consents_texts import CANONICAL_DOCUMENTS
from src.core.enums import Audience
from src.core.security import create_access_token
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

AGENCY_TYPES = ("agency_terms", "agency_dpa")
CLIENT_TYPES = ("client_terms", "client_privacy")


@pytest.fixture
def cs_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


@pytest_asyncio.fixture
async def consent_docs(db_session: AsyncSession) -> None:
    await seed_consent_documents(db_session)


def _expat_headers(expat: ExpatUser) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(str(expat.id), Audience.EXPAT)}"}


async def _accept_agent_docs(
    client: AsyncClient, headers: dict[str, str], version: int = 1
) -> None:
    for doc_type in AGENCY_TYPES:
        r = await client.post(
            "/consents/agent/accept",
            headers=headers,
            json={"document_type": doc_type, "document_version": version},
        )
        assert r.status_code == 200, r.text


async def _accept_expat_docs(
    client: AsyncClient, headers: dict[str, str], agency_id: uuid.UUID, version: int = 1
) -> None:
    for doc_type in CLIENT_TYPES:
        r = await client.post(
            "/consents/expat/accept",
            headers=headers,
            json={
                "document_type": doc_type,
                "document_version": version,
                "agency_id": str(agency_id),
            },
        )
        assert r.status_code == 200, r.text


async def _publish_v2(db_session: AsyncSession, doc_type: str) -> None:
    """Script-style publication: v2 active, v1 toggled off (no endpoint
    at the MVP, by design)."""
    v1 = (
        await db_session.execute(
            select(ConsentDocument).where(
                ConsentDocument.type == doc_type, ConsentDocument.version == 1
            )
        )
    ).scalar_one()
    v1.is_active = False
    content = "# Version 2\n\n[TEXTE PROVISOIRE, a remplacer]\n"
    db_session.add(
        ConsentDocument(
            type=doc_type,
            version=2,
            content_md=content,
            content_hash=content_sha256(content),
            published_at=datetime.now(UTC),
            is_active=True,
        )
    )
    await db_session.commit()


# --- (a) admin gated everywhere but the allowlist --------------------------------------


async def test_admin_gated_until_both_documents_accepted(
    cs_client: AsyncClient, consent_docs: None, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)

    blocked = await cs_client.get("/cases", headers=headers)
    assert blocked.status_code == 403
    body = blocked.json()
    assert body["code"] == "consent.required"
    assert {m["type"] for m in body["params"]["missing"]} == set(AGENCY_TYPES)
    assert all(m["version"] == 1 for m in body["params"]["missing"])

    # The allowlist: identity + the consent flow itself stay reachable.
    assert (await cs_client.get("/auth/agent/me", headers=headers)).status_code == 200
    pending = await cs_client.get("/consents/agent/pending", headers=headers)
    assert pending.status_code == 200
    docs = pending.json()
    assert {d["type"] for d in docs} == set(AGENCY_TYPES)
    # Definitive texts (passation 2026-07-02): the provisional marker is gone.
    assert all("BETTERSOFT LLC" in d["content"] for d in docs)
    assert all("[TEXTE PROVISOIRE" not in d["content"] for d in docs)

    # One of two accepted: still gated, on the remaining one only.
    first = await cs_client.post(
        "/consents/agent/accept",
        headers=headers,
        json={"document_type": "agency_terms", "document_version": 1},
    )
    assert first.status_code == 200
    assert first.json()["already_accepted"] is False
    still = await cs_client.get("/cases", headers=headers)
    assert still.status_code == 403
    assert [m["type"] for m in still.json()["params"]["missing"]] == ["agency_dpa"]

    second = await cs_client.post(
        "/consents/agent/accept",
        headers=headers,
        json={"document_type": "agency_dpa", "document_version": 1},
    )
    assert second.status_code == 200
    assert (await cs_client.get("/cases", headers=headers)).status_code == 200


# --- (b) non-admin agents pass free ----------------------------------------------------


async def test_non_admin_agent_never_gated(
    cs_client: AsyncClient,
    consent_docs: None,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    member = await make_agent(agency_id=admin.agency_id, role=system_roles["member"])
    headers = agent_headers(member)
    assert (await cs_client.get("/cases", headers=headers)).status_code == 200
    assert (await cs_client.get("/consents/agent/pending", headers=headers)).json() == []
    # And a member cannot sign for the agency.
    refused = await cs_client.post(
        "/consents/agent/accept",
        headers=headers,
        json={"document_type": "agency_terms", "document_version": 1},
    )
    assert refused.status_code == 403
    assert refused.json()["code"] == "consent.admin_only"


# --- (c) expat first login -------------------------------------------------------------


async def test_expat_first_login_gated_then_accepts(
    cs_client: AsyncClient,
    db_session: AsyncSession,
    consent_docs: None,
    admin: Agent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
) -> None:
    expat = await make_expat_user()
    case = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)
    headers = _expat_headers(expat)

    blocked = await cs_client.get("/expat/cases", headers=headers)
    assert blocked.status_code == 403
    body = blocked.json()
    assert body["code"] == "consent.required"
    assert {m["type"] for m in body["params"]["missing"]} == set(CLIENT_TYPES)
    assert {m["agency_id"] for m in body["params"]["missing"]} == {str(case.agency_id)}

    assert (await cs_client.get("/auth/expat/me", headers=headers)).status_code == 200
    pending = await cs_client.get("/consents/expat/pending", headers=headers)
    assert pending.status_code == 200
    groups = pending.json()
    assert len(groups) == 1
    agency = await db_session.get(Agency, case.agency_id)
    assert agency is not None
    assert groups[0]["agency_name"] == agency.name
    contents = [d["content"] for d in groups[0]["documents"]]
    # {agency_name} resolved at read time, never leaked raw.
    assert all("{agency_name}" not in c for c in contents)
    assert any(agency.name in c for c in contents)

    await _accept_expat_docs(cs_client, headers, case.agency_id)
    assert (await cs_client.get("/expat/cases", headers=headers)).status_code == 200


# --- (d) expat at two agencies: gated per agency ----------------------------------------


async def test_expat_two_agencies_accepts_per_agency(
    cs_client: AsyncClient,
    db_session: AsyncSession,
    consent_docs: None,
    admin: Agent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
) -> None:
    expat = await make_expat_user()
    case_a = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)
    case_b = await make_client_case(principal_expat_user_id=expat.id)  # its own other agency
    headers = _expat_headers(expat)

    blocked = await cs_client.get("/expat/cases", headers=headers)
    missing = blocked.json()["params"]["missing"]
    assert {m["agency_id"] for m in missing} == {str(case_a.agency_id), str(case_b.agency_id)}
    assert len(missing) == 4  # 2 documents x 2 agencies

    # Agency A accepted: still gated, by agency B alone.
    await _accept_expat_docs(cs_client, headers, case_a.agency_id)
    still = await cs_client.get("/expat/cases", headers=headers)
    assert still.status_code == 403
    assert {m["agency_id"] for m in still.json()["params"]["missing"]} == {str(case_b.agency_id)}

    await _accept_expat_docs(cs_client, headers, case_b.agency_id)
    assert (await cs_client.get("/expat/cases", headers=headers)).status_code == 200

    # Distinct traces: one per (document, agency).
    rows = (
        (
            await db_session.execute(
                select(ConsentAcceptance).where(ConsentAcceptance.actor_id == expat.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 4
    assert {row.agency_id for row in rows} == {case_a.agency_id, case_b.agency_id}


# --- (e) publishing a new version re-gates ----------------------------------------------


async def test_new_version_regates_automatically(
    cs_client: AsyncClient,
    db_session: AsyncSession,
    consent_docs: None,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    await _accept_agent_docs(cs_client, headers)
    assert (await cs_client.get("/cases", headers=headers)).status_code == 200

    await _publish_v2(db_session, "agency_terms")

    regated = await cs_client.get("/cases", headers=headers)
    assert regated.status_code == 403
    assert regated.json()["params"]["missing"] == [{"type": "agency_terms", "version": 2}]

    accept_v2 = await cs_client.post(
        "/consents/agent/accept",
        headers=headers,
        json={"document_type": "agency_terms", "document_version": 2},
    )
    assert accept_v2.status_code == 200
    assert (await cs_client.get("/cases", headers=headers)).status_code == 200


# --- (f) stale version refused -----------------------------------------------------------


async def test_accepting_stale_version_409(
    cs_client: AsyncClient,
    db_session: AsyncSession,
    consent_docs: None,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    await _publish_v2(db_session, "agency_terms")

    stale = await cs_client.post(
        "/consents/agent/accept",
        headers=headers,
        json={"document_type": "agency_terms", "document_version": 1},
    )
    assert stale.status_code == 409
    body = stale.json()
    assert body["code"] == "consent.version_stale"
    assert body["params"] == {"type": "agency_terms", "requested_version": 1, "active_version": 2}

    # Wrong audience: an agency document is not acceptable on the expat face.
    wrong = await cs_client.post(
        "/consents/agent/accept",
        headers=headers,
        json={"document_type": "client_terms", "document_version": 1},
    )
    assert wrong.status_code == 422
    assert wrong.json()["code"] == "consent.wrong_audience"


# --- (g) impersonation: reads pass without the client's consent --------------------------


async def test_impersonation_reads_pass_without_client_consent(
    cs_client: AsyncClient,
    consent_docs: None,
    admin: Agent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """The agent CONSULTS, they are not the client: no consent demanded
    under the mask. And accepting under the mask is structurally
    impossible already (point-12 read-only), nothing more to code."""
    headers = agent_headers(admin)
    await _accept_agent_docs(cs_client, headers)  # the admin's own consent
    expat = await make_expat_user()
    case = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)

    issued = await cs_client.post(f"/expat-users/{expat.id}/impersonate", headers=headers)
    assert issued.status_code == 200
    mask = {"Authorization": f"Bearer {issued.json()['access_token']}"}

    # The client NEVER consented, yet the mask sees their space.
    assert (await cs_client.get("/expat/cases", headers=mask)).status_code == 200
    assert (await cs_client.get(f"/expat/cases/{case.id}", headers=mask)).status_code == 200

    # Accepting under the mask: killed by the point-12 read-only wall.
    forged = await cs_client.post(
        "/consents/expat/accept",
        headers=mask,
        json={
            "document_type": "client_terms",
            "document_version": 1,
            "agency_id": str(case.agency_id),
        },
    )
    assert forged.status_code == 403
    assert forged.json()["code"] == "impersonation.read_only"


# --- (h) the trace: ip + hash + server timestamp, immutable -------------------------------


async def test_acceptance_trace_ip_hash_timestamp_immutable(
    cs_client: AsyncClient,
    db_session: AsyncSession,
    consent_docs: None,
    admin: Agent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
) -> None:
    expat = await make_expat_user()
    case = await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)
    headers = {
        **_expat_headers(expat),
        # Fly-style chain: the FIRST hop is the original client.
        "X-Forwarded-For": "203.0.113.9, 198.51.100.7",
    }

    first = await cs_client.post(
        "/consents/expat/accept",
        headers=headers,
        json={
            "document_type": "client_terms",
            "document_version": 1,
            "agency_id": str(case.agency_id),
        },
    )
    assert first.status_code == 200
    assert first.json()["already_accepted"] is False

    row = (
        await db_session.execute(
            select(ConsentAcceptance).where(
                ConsentAcceptance.actor_id == expat.id,
                ConsentAcceptance.document_type == "client_terms",
            )
        )
    ).scalar_one()
    assert row.ip == "203.0.113.9"
    assert row.content_hash == content_sha256(CANONICAL_DOCUMENTS["client_terms"])
    assert row.accepted_at is not None and row.accepted_at.tzinfo is not None
    original_accepted_at = row.accepted_at
    original_ip = row.ip

    # Idempotent re-acceptance: clean no-op, the original trace untouched
    # (insert-only table: no update path exists anywhere in the code).
    again = await cs_client.post(
        "/consents/expat/accept",
        headers={**_expat_headers(expat), "X-Forwarded-For": "198.51.100.99"},
        json={
            "document_type": "client_terms",
            "document_version": 1,
            "agency_id": str(case.agency_id),
        },
    )
    assert again.status_code == 200
    assert again.json()["already_accepted"] is True

    rows = (
        (
            await db_session.execute(
                select(ConsentAcceptance).where(
                    ConsentAcceptance.actor_id == expat.id,
                    ConsentAcceptance.document_type == "client_terms",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    await db_session.refresh(rows[0])
    assert rows[0].accepted_at == original_accepted_at
    assert rows[0].ip == original_ip


# --- canonical reconcile (passation: definitive texts replace accepted v1) --------------


async def test_seed_publishes_new_version_when_canonical_text_changes(
    cs_client: AsyncClient,
    db_session: AsyncSession,
    consent_docs: None,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Prod scenario: v1 (old placeholder) was ACCEPTED, then the
    canonical texts changed in code. The reconcile publishes v2 for the
    DRIFTED type only, never touches the accepted v1 row (evidentiary
    trace), re-gates the admin on that document alone, and a second run
    is a strict no-op."""
    headers = agent_headers(admin)
    await _accept_agent_docs(cs_client, headers)
    assert (await cs_client.get("/cases", headers=headers)).status_code == 200

    # Simulate the legacy state: v1 of agency_terms held a different text.
    v1 = (
        await db_session.execute(
            select(ConsentDocument).where(
                ConsentDocument.type == "agency_terms", ConsentDocument.version == 1
            )
        )
    ).scalar_one()
    v1.content_md = "# Ancien texte provisoire\n"
    v1.content_hash = content_sha256(v1.content_md)
    await db_session.commit()

    await seed_consent_documents(db_session)

    rows = (
        (
            await db_session.execute(
                select(ConsentDocument)
                .where(ConsentDocument.type == "agency_terms")
                .order_by(ConsentDocument.version)
            )
        )
        .scalars()
        .all()
    )
    assert [(r.version, r.is_active) for r in rows] == [(1, False), (2, True)]
    assert rows[0].content_md == "# Ancien texte provisoire\n"  # accepted v1 untouched
    assert rows[1].content_hash == content_sha256(CANONICAL_DOCUMENTS["agency_terms"])

    # Re-gated on the republished document alone.
    regated = await cs_client.get("/cases", headers=headers)
    assert regated.status_code == 403
    assert regated.json()["params"]["missing"] == [{"type": "agency_terms", "version": 2}]

    # Strict idempotence: a second reconcile writes nothing.
    await seed_consent_documents(db_session)
    count = len((await db_session.execute(select(ConsentDocument))).scalars().all())
    assert count == 6  # 5 types + the one republished version
