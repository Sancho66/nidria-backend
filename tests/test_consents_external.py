"""Provider consent (external_terms) — the gate before the /external
portal, reusing the whole clickwrap machinery.

Covers: (a) first connection blocked (external.consent.required),
accept then pass; (b) a person working for two agencies has two
provider accounts, each gated for its own agency, two immutable traces;
(c) the external allowlist (identity + consent read/accept) is reachable
before consent; (d) publishing a new version re-gates; (e) the AGENT and
EXPAT gates are unchanged (external_terms never leaks into their pending
sets); (f) the external trace carries actor_type EXTERNAL, agency_id,
content_hash, ip and timestamp."""

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.consent import ConsentAcceptance, ConsentDocument
from shared.models.rbac import Role
from src.consents.consents_seed import content_sha256, seed_consent_documents
from src.consents.consents_texts import _EXTERNAL_TERMS
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def consent_docs(db_session: AsyncSession) -> None:
    await seed_consent_documents(db_session)


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
async def provider(make_agent: MakeAgent, admin: Agent, external_role: Role) -> Agent:
    return await make_agent(
        agency_id=admin.agency_id, role=external_role, is_external=True, email="lawyer@ext.com"
    )


def _accept_body(version: int = 1) -> dict:
    return {"document_type": "external_terms", "document_version": version}


# --- (a) + (c) gate then pass, allowlist reachable ------------------------------------


async def test_provider_gated_until_terms_accepted(
    client: AsyncClient,
    consent_docs: None,
    provider: Agent,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(provider)

    # (c) the consent read/accept + identity routes are reachable BEFORE
    # consent (allowlist + CONSENT_EXEMPT).
    pending = await client.get("/consents/external/pending", headers=headers)
    assert pending.status_code == 200, pending.text
    docs = pending.json()
    assert len(docs) == 1
    assert docs[0]["agency_id"] == str(provider.agency_id)
    assert [d["type"] for d in docs[0]["documents"]] == ["external_terms"]
    assert "{agency_name}" not in docs[0]["documents"][0]["content"]  # token resolved
    assert (await client.get("/auth/agent/me", headers=headers)).status_code == 200

    # (a) the portal itself is blocked with the dedicated code.
    blocked = await client.get("/external/cases", headers=headers)
    assert blocked.status_code == 403
    body = blocked.json()
    assert body["code"] == "external.consent.required"
    assert body["params"]["missing"][0]["type"] == "external_terms"
    assert body["params"]["missing"][0]["agency_id"] == str(provider.agency_id)

    accepted = await client.post("/consents/external/accept", headers=headers, json=_accept_body())
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["already_accepted"] is False

    # The portal opens; pending is now empty; a second accept is idempotent.
    assert (await client.get("/external/cases", headers=headers)).status_code == 200
    assert (await client.get("/consents/external/pending", headers=headers)).json() == []
    again = await client.post("/consents/external/accept", headers=headers, json=_accept_body())
    assert again.json()["already_accepted"] is True


# --- (b) two agencies -> per-agency gate, two traces ---------------------------------


async def test_two_agencies_gate_and_trace_per_agency(
    client: AsyncClient,
    db_session: AsyncSession,
    consent_docs: None,
    provider: Agent,
    make_agent: MakeAgent,
    external_role: Role,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """A provider Agent belongs to ONE agency (get_case_for_external
    never crosses it), so a person at two agencies has two accounts -
    each gated for its own agency, each with its own trace."""
    other_admin = await make_agent(role=system_roles["admin"], email="admin2@x.com")
    provider2 = await make_agent(
        agency_id=other_admin.agency_id,
        role=external_role,
        is_external=True,
        email="lawyer@ext2.com",
    )

    # Each provider is gated for ITS agency only.
    for prov in (provider, provider2):
        docs = (await client.get("/consents/external/pending", headers=agent_headers(prov))).json()
        assert [d["agency_id"] for d in docs] == [str(prov.agency_id)]
        accepted = await client.post(
            "/consents/external/accept", headers=agent_headers(prov), json=_accept_body()
        )
        assert accepted.status_code == 200, accepted.text

    # Two immutable traces, one per (provider, agency).
    rows = list(
        (
            await db_session.execute(
                select(ConsentAcceptance).where(
                    ConsentAcceptance.actor_type == "external",
                    ConsentAcceptance.actor_id.in_([provider.id, provider2.id]),
                )
            )
        ).scalars()
    )
    assert len(rows) == 2
    assert {r.agency_id for r in rows} == {provider.agency_id, provider2.agency_id}


# --- (d) new version re-gates ---------------------------------------------------------


async def test_new_version_regates_the_provider(
    client: AsyncClient,
    db_session: AsyncSession,
    consent_docs: None,
    provider: Agent,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(provider)
    await client.post("/consents/external/accept", headers=headers, json=_accept_body())
    assert (await client.get("/external/cases", headers=headers)).status_code == 200

    # Publish v2 (script-style: v1 off, v2 active) -> re-gated.
    v1 = (
        await db_session.execute(
            select(ConsentDocument).where(
                ConsentDocument.type == "external_terms", ConsentDocument.version == 1
            )
        )
    ).scalar_one()
    v1.is_active = False
    content = "# Version 2\n\n[TEXTE PROVISOIRE, a remplacer] {agency_name}\n"
    db_session.add(
        ConsentDocument(
            type="external_terms",
            version=2,
            content_md=content,
            content_hash=content_sha256(content),
            published_at=datetime.now(UTC),
            is_active=True,
        )
    )
    await db_session.commit()

    blocked = await client.get("/external/cases", headers=headers)
    assert blocked.status_code == 403
    assert blocked.json()["params"]["missing"][0]["version"] == 2
    # Accepting v1 again is stale; v2 passes.
    stale = await client.post("/consents/external/accept", headers=headers, json=_accept_body(1))
    assert stale.status_code == 409 and stale.json()["code"] == "consent.version_stale"
    ok = await client.post("/consents/external/accept", headers=headers, json=_accept_body(2))
    assert ok.status_code == 200
    assert (await client.get("/external/cases", headers=headers)).status_code == 200


# --- (e) the AGENT / EXPAT gates are unchanged ---------------------------------------


async def test_agent_and_expat_gates_ignore_external_terms(
    client: AsyncClient,
    consent_docs: None,
    admin: Agent,
    provider: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """external_terms never enters the agent set: the admin's pending is
    exactly the two agency docs, and a non-external agent hitting the
    external accept is refused."""
    admin_pending = (
        await client.get("/consents/agent/pending", headers=agent_headers(admin))
    ).json()
    assert {d["type"] for d in admin_pending} == {"agency_terms", "agency_dpa"}

    refused = await client.post(
        "/consents/external/accept", headers=agent_headers(admin), json=_accept_body()
    )
    assert refused.status_code == 403 and refused.json()["code"] == "consent.wrong_audience"

    # And a provider cannot sign an agency document through its endpoint.
    wrong = await client.post(
        "/consents/external/accept",
        headers=agent_headers(provider),
        json={"document_type": "agency_terms", "document_version": 1},
    )
    assert wrong.status_code == 422 and wrong.json()["code"] == "consent.wrong_audience"


# --- (f) the trace carries ip / hash / timestamp -------------------------------------


async def test_external_trace_is_complete_and_immutable(
    client: AsyncClient,
    db_session: AsyncSession,
    consent_docs: None,
    provider: Agent,
    agent_headers: AuthHeaders,
) -> None:
    headers = {**agent_headers(provider), "x-forwarded-for": "203.0.113.7, 10.0.0.1"}
    before = datetime.now(UTC)
    accepted = await client.post("/consents/external/accept", headers=headers, json=_accept_body())
    assert accepted.status_code == 200, accepted.text

    row = (
        await db_session.execute(
            select(ConsentAcceptance).where(
                ConsentAcceptance.actor_type == "external",
                ConsentAcceptance.actor_id == provider.id,
            )
        )
    ).scalar_one()
    assert row.agency_id == provider.agency_id
    assert row.document_type == "external_terms" and row.document_version == 1
    assert row.content_hash == content_sha256(_EXTERNAL_TERMS)  # hash of the RAW text
    assert row.ip == "203.0.113.7"  # first X-Forwarded-For hop
    assert row.accepted_at >= before  # server clock stamped
