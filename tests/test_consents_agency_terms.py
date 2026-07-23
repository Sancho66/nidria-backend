"""An agency may serve its OWN client terms to ITS clients.

The clickwrap machinery is untouched — the agency's text is published as a
real versioned consent_document, so it gets a hash, a version and the same
automatic re-gating as Nidria's. Only `client_terms` is overridable in this
lot: client_privacy, the two agency documents and the provider terms stay
canonical.

The load-bearing subtlety, pinned below: versions are numbered PER OWNER,
so an agency's v1 and Nidria's v1 coexist. An acceptance therefore records
WHICH text was signed (document_agency_id) — without it, a client who had
accepted Nidria's v1 would be silently considered to have accepted the
agency's brand-new v1.
"""

import uuid

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
from src.core.enums import Audience
from src.core.security import create_access_token
from tests.plugins.agency_plugin import MakeAgency
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

AGENCY_TYPES = ("agency_terms", "agency_dpa")
CLIENT_TYPES = ("client_terms", "client_privacy")

_AGENCY_CGV = "# Conditions générales de MonAgence\n\nArticle 1 : nos propres règles.\n"
_AGENCY_CGV_V2 = "# Conditions générales de MonAgence\n\nArticle 1 : nos règles, revues.\n"


@pytest.fixture
def cs_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def consent_docs(db_session: AsyncSession) -> None:
    await seed_consent_documents(db_session)


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


def _expat_headers(expat: ExpatUser) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(str(expat.id), Audience.EXPAT)}"}


async def _accept_agent_docs(client: AsyncClient, headers: dict[str, str]) -> None:
    """The admin must clear its own gate before it can PATCH the agency."""
    for doc_type in AGENCY_TYPES:
        r = await client.post(
            "/consents/agent/accept",
            headers=headers,
            json={"document_type": doc_type, "document_version": 1},
        )
        assert r.status_code == 200, r.text


async def _set_agency_terms(client: AsyncClient, headers: dict[str, str], text: str | None) -> None:
    r = await client.patch("/agencies/me", headers=headers, json={"client_terms_md": text})
    assert r.status_code == 200, r.text


def _client_terms(pending: list[dict]) -> dict:
    """The client_terms entry of a single agency's pending block."""
    assert len(pending) == 1, pending
    docs = [d for d in pending[0]["documents"] if d["type"] == "client_terms"]
    assert len(docs) == 1, pending
    return docs[0]


# --- non-regression: an agency without its own terms serves Nidria's -----------------


async def test_agency_without_own_terms_serves_the_nidria_text(
    cs_client: AsyncClient,
    consent_docs: None,
    admin: Agent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
) -> None:
    expat = await make_expat_user()
    await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)

    pending = await cs_client.get("/consents/expat/pending", headers=_expat_headers(expat))
    assert pending.status_code == 200
    doc = _client_terms(pending.json())
    # The canonical text, unchanged — the whole fallback in one assertion.
    canonical = (
        await cs_client.get("/consents/expat/pending", headers=_expat_headers(expat))
    ).json()
    assert canonical == pending.json()
    assert "espace client" in doc["content"]
    assert doc["version"] == 1


async def test_agency_settings_expose_no_terms_by_default(
    cs_client: AsyncClient, consent_docs: None, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    await _accept_agent_docs(cs_client, headers)
    me = await cs_client.get("/agencies/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["client_terms_md"] is None  # none = my clients see Nidria's


# --- the lot: an agency's own terms replace Nidria's, for ITS clients ---------------


async def test_agency_own_terms_are_served_to_its_clients(
    cs_client: AsyncClient,
    consent_docs: None,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
) -> None:
    headers = agent_headers(admin)
    await _accept_agent_docs(cs_client, headers)
    await _set_agency_terms(cs_client, headers, _AGENCY_CGV)

    expat = await make_expat_user()
    await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)
    doc = _client_terms(
        (await cs_client.get("/consents/expat/pending", headers=_expat_headers(expat))).json()
    )
    assert doc["content"] == _AGENCY_CGV
    assert doc["content_hash"] == content_sha256(_AGENCY_CGV)
    # A written field is re-readable in Settings.
    assert (await cs_client.get("/agencies/me", headers=headers)).json()[
        "client_terms_md"
    ] == _AGENCY_CGV


async def test_the_other_bundle_documents_stay_canonical(
    cs_client: AsyncClient,
    consent_docs: None,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
) -> None:
    """Only client_terms is overridable in this lot: the privacy notice
    keeps coming from Nidria even for an agency that wrote its own CGV."""
    headers = agent_headers(admin)
    await _accept_agent_docs(cs_client, headers)
    await _set_agency_terms(cs_client, headers, _AGENCY_CGV)

    expat = await make_expat_user()
    await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)
    pending = (await cs_client.get("/consents/expat/pending", headers=_expat_headers(expat))).json()
    privacy = next(d for d in pending[0]["documents"] if d["type"] == "client_privacy")
    assert "Note d'information sur vos données" in privacy["content"]
    assert privacy["content"] != _AGENCY_CGV


# --- traceability: WHICH text was signed --------------------------------------------


async def test_acceptance_records_the_agency_text_and_its_hash(
    cs_client: AsyncClient,
    db_session: AsyncSession,
    consent_docs: None,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
) -> None:
    headers = agent_headers(admin)
    await _accept_agent_docs(cs_client, headers)
    await _set_agency_terms(cs_client, headers, _AGENCY_CGV)

    expat = await make_expat_user()
    await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)
    accepted = await cs_client.post(
        "/consents/expat/accept",
        headers=_expat_headers(expat),
        json={
            "document_type": "client_terms",
            "document_version": 1,
            "agency_id": str(admin.agency_id),
        },
    )
    assert accepted.status_code == 200, accepted.text

    row = (
        await db_session.execute(
            select(ConsentAcceptance).where(
                ConsentAcceptance.actor_id == expat.id,
                ConsentAcceptance.document_type == "client_terms",
            )
        )
    ).scalar_one()
    assert row.content_hash == content_sha256(_AGENCY_CGV)  # WHAT was signed
    assert row.document_agency_id == admin.agency_id  # WHOSE text it was
    assert row.agency_id == admin.agency_id  # for which controller
    assert row.accepted_at is not None  # WHEN


async def test_nidria_acceptance_is_marked_as_canonical(
    cs_client: AsyncClient,
    db_session: AsyncSession,
    consent_docs: None,
    admin: Agent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
) -> None:
    """The other half of the distinction: no agency text → the trace says
    'canonical' (document_agency_id NULL), which is also what every
    pre-existing acceptance means."""
    expat = await make_expat_user()
    await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)
    r = await cs_client.post(
        "/consents/expat/accept",
        headers=_expat_headers(expat),
        json={
            "document_type": "client_terms",
            "document_version": 1,
            "agency_id": str(admin.agency_id),
        },
    )
    assert r.status_code == 200, r.text
    row = (
        await db_session.execute(
            select(ConsentAcceptance).where(
                ConsentAcceptance.actor_id == expat.id,
                ConsentAcceptance.document_type == "client_terms",
            )
        )
    ).scalar_one()
    assert row.document_agency_id is None


# --- versioning: editing the terms re-gates the clients ------------------------------


async def test_editing_agency_terms_regates_its_clients(
    cs_client: AsyncClient,
    db_session: AsyncSession,
    consent_docs: None,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
) -> None:
    headers = agent_headers(admin)
    await _accept_agent_docs(cs_client, headers)
    await _set_agency_terms(cs_client, headers, _AGENCY_CGV)

    expat = await make_expat_user()
    await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)
    expat_h = _expat_headers(expat)
    for doc_type in CLIENT_TYPES:
        r = await cs_client.post(
            "/consents/expat/accept",
            headers=expat_h,
            json={
                "document_type": doc_type,
                "document_version": 1,
                "agency_id": str(admin.agency_id),
            },
        )
        assert r.status_code == 200, r.text
    assert (await cs_client.get("/consents/expat/pending", headers=expat_h)).json() == []

    # The agency revises its CGV → new version → the client is gated again,
    # on client_terms ONLY (the privacy notice stays accepted).
    await _set_agency_terms(cs_client, headers, _AGENCY_CGV_V2)
    pending = (await cs_client.get("/consents/expat/pending", headers=expat_h)).json()
    doc = _client_terms(pending)
    assert doc["version"] == 2
    assert doc["content"] == _AGENCY_CGV_V2

    # The PAST acceptance is untouched, still pointing at what was signed.
    rows = list(
        (
            await db_session.execute(
                select(ConsentAcceptance).where(
                    ConsentAcceptance.actor_id == expat.id,
                    ConsentAcceptance.document_type == "client_terms",
                )
            )
        ).scalars()
    )
    assert len(rows) == 1
    assert rows[0].document_version == 1
    assert rows[0].content_hash == content_sha256(_AGENCY_CGV)


async def test_rewriting_the_same_text_publishes_nothing(
    cs_client: AsyncClient,
    db_session: AsyncSession,
    consent_docs: None,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Idempotent like the canonical seed: an identical text is not a new
    version, so clients are NOT pointlessly re-gated."""
    headers = agent_headers(admin)
    await _accept_agent_docs(cs_client, headers)
    await _set_agency_terms(cs_client, headers, _AGENCY_CGV)
    await _set_agency_terms(cs_client, headers, _AGENCY_CGV)

    versions = list(
        (
            await db_session.execute(
                select(ConsentDocument.version).where(
                    ConsentDocument.type == "client_terms",
                    ConsentDocument.agency_id == admin.agency_id,
                )
            )
        ).scalars()
    )
    assert versions == [1]


async def test_clearing_the_terms_falls_back_to_nidria(
    cs_client: AsyncClient,
    consent_docs: None,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
) -> None:
    """Withdrawing is a first-class move, not a trap: blank the field and
    the clients see the Nidria text again (no blocking, no dead end)."""
    headers = agent_headers(admin)
    await _accept_agent_docs(cs_client, headers)
    await _set_agency_terms(cs_client, headers, _AGENCY_CGV)
    await _set_agency_terms(cs_client, headers, "   ")

    expat = await make_expat_user()
    await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)
    doc = _client_terms(
        (await cs_client.get("/consents/expat/pending", headers=_expat_headers(expat))).json()
    )
    assert "espace client" in doc["content"]
    assert doc["content"] != _AGENCY_CGV
    assert (await cs_client.get("/agencies/me", headers=headers)).json()["client_terms_md"] is None


async def test_accepted_nidria_v1_does_not_satisfy_the_agency_v1(
    cs_client: AsyncClient,
    consent_docs: None,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
) -> None:
    """THE trap this design exists to avoid. Both sequences number from 1:
    a client who accepted Nidria's client_terms v1 must be re-gated when
    the agency publishes ITS v1 — same type, same version number, different
    document."""
    headers = agent_headers(admin)
    await _accept_agent_docs(cs_client, headers)

    expat = await make_expat_user()
    await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)
    expat_h = _expat_headers(expat)
    for doc_type in CLIENT_TYPES:
        r = await cs_client.post(
            "/consents/expat/accept",
            headers=expat_h,
            json={
                "document_type": doc_type,
                "document_version": 1,
                "agency_id": str(admin.agency_id),
            },
        )
        assert r.status_code == 200, r.text
    assert (await cs_client.get("/consents/expat/pending", headers=expat_h)).json() == []

    await _set_agency_terms(cs_client, headers, _AGENCY_CGV)

    pending = (await cs_client.get("/consents/expat/pending", headers=expat_h)).json()
    doc = _client_terms(pending)
    assert doc["version"] == 1  # the AGENCY's v1...
    assert doc["content"] == _AGENCY_CGV  # ...which is a different text
    # And it is acceptable: the client is not stuck on a phantom document.
    accepted = await cs_client.post(
        "/consents/expat/accept",
        headers=expat_h,
        json={
            "document_type": "client_terms",
            "document_version": 1,
            "agency_id": str(admin.agency_id),
        },
    )
    assert accepted.status_code == 200, accepted.text
    assert (await cs_client.get("/consents/expat/pending", headers=expat_h)).json() == []


# --- multi-tenant isolation ----------------------------------------------------------


async def test_another_agencys_clients_are_unaffected(
    cs_client: AsyncClient,
    consent_docs: None,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_agency: MakeAgency,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
) -> None:
    """Agency A publishes its own CGV; agency B's clients keep Nidria's,
    and a client of BOTH sees each agency's own text in its own block."""
    headers = agent_headers(admin)
    await _accept_agent_docs(cs_client, headers)
    await _set_agency_terms(cs_client, headers, _AGENCY_CGV)

    other_agency: Agency = await make_agency()
    other_admin = await make_agent(agency_id=other_agency.id, role=system_roles["admin"])
    assert other_admin.agency_id == other_agency.id

    expat = await make_expat_user()
    await make_client_case(agency_id=other_agency.id, principal_expat_user_id=expat.id)
    only_b = (await cs_client.get("/consents/expat/pending", headers=_expat_headers(expat))).json()
    doc_b = _client_terms(only_b)
    assert doc_b["content"] != _AGENCY_CGV  # B never inherits A's text
    assert "espace client" in doc_b["content"]

    # The same client also joins agency A: two blocks, two different texts.
    await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)
    both = (await cs_client.get("/consents/expat/pending", headers=_expat_headers(expat))).json()
    by_agency: dict[uuid.UUID, str] = {
        uuid.UUID(block["agency_id"]): next(
            d["content"] for d in block["documents"] if d["type"] == "client_terms"
        )
        for block in both
    }
    assert by_agency[admin.agency_id] == _AGENCY_CGV
    assert by_agency[other_agency.id] != _AGENCY_CGV
