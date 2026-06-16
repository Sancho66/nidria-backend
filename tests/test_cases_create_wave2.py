"""Transactional case creation (champs par parcours, VAGUE 2) — POST
/cases enriched with an OPTIONAL journey + the principal's OPTIONAL
values, all in one atomic transaction.

Covers: strict retrocompat (nu-case unchanged), enriched creation
(journey assigned + principal values in one call), atomicity (an invalid
value → 422 with NO orphan case), required_at_creation NO LONGER blocking
(vague F repurpose — it became a non-blocking completeness indicator on the
case detail), and cross-agency template scoping (404)."""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent


@pytest.fixture
def w2_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    """admin holds journey.configure + case.edit + field.manage."""
    return await make_agent(role=system_roles["admin"])


def _payload(email_addr: str = "client@example.com", **overrides: object) -> dict[str, object]:
    return {
        "first_name": "Jean",
        "last_name": "Martin",
        "email": email_addr,
        "origin_country": "FR",
        "dest_country": "PY",
        **overrides,
    }


async def _template_with_field(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    kind: str,
    reference: str,
    required: bool,
) -> str:
    tid = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    r = await client.post(
        f"/journeys/{tid}/fields",
        headers=headers,
        json={"kind": kind, "reference": reference, "required_at_creation": required},
    )
    assert r.status_code == 201, r.text
    return tid


async def _custom_field(client: AsyncClient, headers: dict[str, str], **body: object) -> dict:
    r = await client.post("/agencies/me/custom-fields", headers=headers, json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def _template_with_case_field(
    client: AsyncClient, headers: dict[str, str], *, case_field: str, required: bool
) -> str:
    tid = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    r = await client.post(
        f"/journeys/{tid}/case-fields",
        headers=headers,
        json={"case_field": case_field, "required_at_creation": required},
    )
    assert r.status_code == 201, r.text
    return tid


# --- strict retrocompat --------------------------------------------------------------


async def test_create_case_nu_unchanged(
    w2_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """The current call (no journey, no values) behaves exactly as before:
    nu case, principal with only identity, no progress."""
    headers = agent_headers(admin)
    resp = await w2_client.post("/cases", headers=headers, json=_payload("nu@example.com"))
    assert resp.status_code == 201
    body = resp.json()
    assert body["journey_template_id"] is None

    detail = (await w2_client.get(f"/cases/{body['id']}", headers=headers)).json()
    assert detail["progress"] == []
    principal = next(p for p in detail["persons"] if p["kind"] == "principal")
    assert principal["passport_number"] is None
    assert principal["custom_fields"] == {}


# --- enriched creation ---------------------------------------------------------------


async def test_create_case_with_journey_and_values_one_call(
    w2_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    cf = await _custom_field(w2_client, headers, key="visa_type", label="Visa", field_type="text")
    assert cf["key"] == "visa_type"
    tid = (await w2_client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    await w2_client.post(f"/journeys/{tid}/steps", headers=headers, json={"name": "S1"})

    resp = await w2_client.post(
        "/cases",
        headers=headers,
        json=_payload(
            "rich@example.com",
            journey_template_id=tid,
            passport_number="AB12345",
            custom_fields={"visa_type": "work"},
        ),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # Journey assigned in the same call.
    assert body["journey_template_id"] == tid
    detail = (await w2_client.get(f"/cases/{body['id']}", headers=headers)).json()
    assert len(detail["progress"]) == 1  # the single step instantiated
    # Principal values landed.
    principal = next(p for p in detail["persons"] if p["kind"] == "principal")
    assert principal["passport_number"] == "AB12345"
    assert principal["custom_fields"] == {"visa_type": "work"}


# --- atomicity -----------------------------------------------------------------------


async def test_create_case_invalid_value_is_atomic_no_orphan(
    w2_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """An invalid custom value → 422 and NOTHING is created: no orphan
    case, no orphan expat (the whole POST rolls back)."""
    headers = agent_headers(admin)
    # Capture the id now — rollback() below expires the ORM object, and
    # re-reading admin.agency_id would trigger an async lazy-load.
    agency_id = admin.agency_id
    before_cases = (
        await db_session.execute(
            select(func.count()).select_from(ClientCase).where(ClientCase.agency_id == agency_id)
        )
    ).scalar_one()

    resp = await w2_client.post(
        "/cases",
        headers=headers,
        json=_payload("orphan@example.com", custom_fields={"ghost": "x"}),  # unknown key
    )
    assert resp.status_code == 422

    # The harness shares ONE session with the request and does not roll
    # back on exception, so the failed POST leaves its INSERTs flushed but
    # UNCOMMITTED. Roll them back here, then count: if the manager had
    # (wrongly) committed before raising, rollback would NOT undo it and
    # the count would be non-zero. count==0 proves nothing was committed
    # (in prod, get_db's `async with` rolls back on the same exception).
    await db_session.rollback()
    after_cases = (
        await db_session.execute(
            select(func.count()).select_from(ClientCase).where(ClientCase.agency_id == agency_id)
        )
    ).scalar_one()
    assert after_cases == before_cases  # no orphan case
    # The expat row would be created before the failing value — assert it
    # was rolled back too (the whole transaction is atomic).
    expat = (
        await db_session.execute(select(ExpatUser).where(ExpatUser.email == "orphan@example.com"))
    ).scalar_one_or_none()
    assert expat is None


# --- required_at_creation ------------------------------------------------------------


async def test_required_at_creation_no_longer_blocks(
    w2_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """Vague F: required_at_creation became a non-blocking completeness
    indicator (surfaced on the case detail). A missing required value NEVER
    blocks creation now — the modal is socle-only."""
    headers = agent_headers(admin)
    tid = await _template_with_field(
        w2_client, headers, kind="base_field", reference="passport_number", required=True
    )
    # Missing the required value → still 201 (no enforcement).
    missing = await w2_client.post(
        "/cases",
        headers=headers,
        json=_payload("req1@example.com", journey_template_id=tid),
    )
    assert missing.status_code == 201, missing.text

    # With the value → 201 too.
    ok = await w2_client.post(
        "/cases",
        headers=headers,
        json=_payload("req2@example.com", journey_template_id=tid, passport_number="X1"),
    )
    assert ok.status_code == 201


async def test_required_at_creation_not_enforced_without_journey(
    w2_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """A template flags a field required, but no journey is assigned on
    this POST → no enforcement (retrocompat: a nu case requires nothing)."""
    headers = agent_headers(admin)
    await _template_with_field(
        w2_client, headers, kind="base_field", reference="passport_number", required=True
    )
    resp = await w2_client.post("/cases", headers=headers, json=_payload("nojourney@example.com"))
    assert resp.status_code == 201


async def test_required_at_creation_archived_custom_does_not_block(
    w2_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """A custom field flagged required_at_creation, then its definition is
    archived: it has dropped off the picker → it must NOT block creation."""
    headers = agent_headers(admin)
    cf = await _custom_field(w2_client, headers, key="old_visa", label="Old", field_type="text")
    tid = await _template_with_field(
        w2_client, headers, kind="custom_field", reference="old_visa", required=True
    )
    await w2_client.post(f"/agencies/me/custom-fields/{cf['id']}/archive", headers=headers)

    resp = await w2_client.post(
        "/cases",
        headers=headers,
        json=_payload("archived@example.com", journey_template_id=tid),
    )
    assert resp.status_code == 201


# --- scoping -------------------------------------------------------------------------


async def test_create_case_foreign_template_404(
    w2_client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """A journey_template_id of ANOTHER agency is invisible → 404, nothing
    created."""
    other_admin = await make_agent(role=system_roles["admin"])
    foreign_tid = (
        await w2_client.post(
            "/journeys", headers=agent_headers(other_admin), json={"name": "Foreign"}
        )
    ).json()["id"]

    resp = await w2_client.post(
        "/cases",
        headers=agent_headers(admin),
        json=_payload("xagency@example.com", journey_template_id=foreign_tid),
    )
    assert resp.status_code == 404


async def test_create_case_unknown_template_404(
    w2_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    resp = await w2_client.post(
        "/cases",
        headers=agent_headers(admin),
        json=_payload("ghost-tmpl@example.com", journey_template_id=str(uuid.uuid4())),
    )
    assert resp.status_code == 404


# --- case-level required fields (countries, option b) --------------------------------


async def test_required_case_field_no_longer_blocks(
    w2_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """Vague F: a required case-field (country) no longer blocks creation —
    the case is created even without the value (completeness indicator)."""
    headers = agent_headers(admin)
    tid = await _template_with_case_field(
        w2_client, headers, case_field="origin_country", required=True
    )
    # origin_country=None overrides the _payload default of "FR".
    missing = await w2_client.post(
        "/cases",
        headers=headers,
        json=_payload("noctry@example.com", journey_template_id=tid, origin_country=None),
    )
    assert missing.status_code == 201, missing.text

    await db_session.rollback()
    case = (
        await db_session.execute(
            select(ClientCase).where(ClientCase.id == uuid.UUID(missing.json()["id"]))
        )
    ).scalar_one()
    assert case.origin_country is None  # created without the required value


async def test_required_case_field_with_country_creates_and_persists(
    w2_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    tid = await _template_with_case_field(
        w2_client, headers, case_field="origin_country", required=True
    )
    resp = await w2_client.post(
        "/cases",
        headers=headers,
        json=_payload("withctry@example.com", journey_template_id=tid, origin_country="FR"),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # The value lives on client_case (NOT moved anywhere) — read it back.
    case = (
        await db_session.execute(select(ClientCase).where(ClientCase.id == uuid.UUID(body["id"])))
    ).scalar_one()
    assert case.origin_country == "FR"


async def test_required_case_field_not_enforced_without_journey(
    w2_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """A template requires origin_country, but no journey is assigned on
    this POST → no enforcement (retrocompat)."""
    headers = agent_headers(admin)
    await _template_with_case_field(w2_client, headers, case_field="origin_country", required=True)
    resp = await w2_client.post(
        "/cases", headers=headers, json=_payload("nojourney-ctry@example.com", origin_country=None)
    )
    assert resp.status_code == 201


# --- full address case-fields (vague B) ----------------------------------------------


async def test_create_case_writes_address_value_to_client_case(
    w2_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """An address value (origin_city) passed at creation lands on
    client_case via the existing top-level key — no new write path."""
    headers = agent_headers(admin)
    resp = await w2_client.post(
        "/cases",
        headers=headers,
        json=_payload("addr@example.com", origin_city="Paris", origin_street="1 rue de Rivoli"),
    )
    assert resp.status_code == 201, resp.text
    case = (
        await db_session.execute(
            select(ClientCase).where(ClientCase.id == uuid.UUID(resp.json()["id"]))
        )
    ).scalar_one()
    assert case.origin_city == "Paris"
    assert case.origin_street == "1 rue de Rivoli"


async def test_required_address_field_no_longer_blocks(
    w2_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """Vague F: a required address case-field no longer blocks creation
    either (same repurpose as the country/person required fields)."""
    headers = agent_headers(admin)
    tid = await _template_with_case_field(
        w2_client, headers, case_field="origin_city", required=True
    )
    missing = await w2_client.post(
        "/cases", headers=headers, json=_payload("nocity@example.com", journey_template_id=tid)
    )
    assert missing.status_code == 201, missing.text
    ok = await w2_client.post(
        "/cases",
        headers=headers,
        json=_payload("withcity@example.com", journey_template_id=tid, origin_city="Lyon"),
    )
    assert ok.status_code == 201
