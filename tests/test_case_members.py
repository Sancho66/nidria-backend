"""Dossier MEMBER (family/associate) read-only access (Nicolas' request).

The agency adds a member with an OPTIONAL email → a read-only account (a
case_person carrying an expat_user_id, the SAME global pivot as the principal,
linked-or-created by email). The member sees the dossier PROGRESS and their OWN
requirements/documents only — never the principal's civil fields, passport, or
documents, never a step attachment. Read-only is a property of the LINK: every
expat write path resolves the principal, so a member is 404 on all of them.
No 4th entity, no migration.
"""

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.case_person import CasePerson
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser


@pytest.fixture
def mem_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


async def _add_field_req(
    client: AsyncClient, headers: dict, tid: str, sid: str, reference: str, scope: str
) -> None:
    await client.post(
        f"/journeys/{tid}/fields",
        headers=headers,
        json={"kind": "base_field", "reference": reference},
    )
    r = await client.post(
        f"/journeys/{tid}/steps/{sid}/requirements",
        headers=headers,
        json={"kind": "base_field", "reference": reference, "scope": scope},
    )
    assert r.status_code == 201, r.text


async def _principal_person_id(db: AsyncSession, case_id: uuid.UUID) -> uuid.UUID:
    return (
        await db.execute(
            select(CasePerson.id).where(
                CasePerson.case_id == case_id, CasePerson.kind == "principal"
            )
        )
    ).scalar_one()


def _all_requirements(detail: dict) -> list[dict]:
    return [req for step in detail["timeline"] for req in step["requirements"]]


# --- (1) a member sees the progress + their own requirements, never the principal's --


async def test_member_sees_own_requirements_not_the_principal_civil(
    mem_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    principal = await make_expat_user(email="principal@x.io")
    member = await make_expat_user(email="member@x.io")
    case_id, _ = await _setup_with_db(
        mem_client, db_session, admin, headers, make_client_case, principal, member.email
    )

    detail = (await mem_client.get(f"/expat/cases/{case_id}", headers=expat_headers(member))).json()
    assert detail["viewer_role"] == "member"
    reqs = _all_requirements(detail)
    # The member sees exactly ONE requirement — their OWN date_of_birth. The
    # principal's date_of_birth (other person) and passport (principal scope)
    # are absent; no value ever leaks the principal's civil data.
    assert {r["reference"] for r in reqs} == {"date_of_birth"}
    assert all(r["person_label"] == "Marie Dupont" for r in reqs)
    assert not any(r["reference"] == "passport_number" for r in reqs)
    assert not any(r["value"] == "X-SECRET-42" for r in reqs)
    # The member still sees the dossier PROGRESS (the step is there).
    assert len(detail["timeline"]) == 1


# --- (7) the principal keeps seeing everything (non-regression) -----------------------


async def test_principal_still_sees_everything(
    mem_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    principal = await make_expat_user(email="principal@x.io")
    member = await make_expat_user(email="member@x.io")
    case_id, _ = await _setup_with_db(
        mem_client, db_session, admin, headers, make_client_case, principal, member.email
    )

    detail = (
        await mem_client.get(f"/expat/cases/{case_id}", headers=expat_headers(principal))
    ).json()
    assert detail["viewer_role"] == "principal"
    refs = {r["reference"] for r in _all_requirements(detail)}
    # Principal scope + each_person for both persons: passport + 2×dob.
    assert refs == {"passport_number", "date_of_birth"}
    assert any(r["value"] == "X-SECRET-42" for r in _all_requirements(detail))


# --- (3) a member cannot write ANYWHERE — by COMPREHENSION over the routes -----------
#
# We enumerate every EXPAT-face route whose method is not GET and assert a
# member is 404 on each. A future expat write endpoint is covered the day it is
# added — it is born locked, exactly like the impersonation write mask. If this
# list ever shrinks to nothing (a broken filter), the count guard fails loudly.


def _expat_write_routes() -> list[tuple[str, str]]:
    from fastapi.routing import APIRoute

    from src.main import app

    writes: set[tuple[str, str]] = set()
    for route in app.routes:
        # The client PORTAL prefix "/expat/" — NOT "/expat-users/..." (the
        # agent-audience impersonation mint), which "/expat" would wrongly catch.
        if not isinstance(route, APIRoute) or not route.path.startswith("/expat/"):
            continue
        for method in route.methods:
            if method not in ("GET", "HEAD", "OPTIONS"):
                writes.add((method, route.path))
    return sorted(writes)


def _fill_path(template: str, case_id: uuid.UUID) -> str:
    path = template.replace("{case_id}", str(case_id))
    while "{" in path:
        head, _, rest = path.partition("{")
        _param, _, tail = rest.partition("}")
        path = f"{head}{uuid.uuid4()}{tail}"
    return path


async def test_member_cannot_write_on_any_expat_route(
    mem_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    principal = await make_expat_user(email="principal@x.io")
    member = await make_expat_user(email="member@x.io")
    case_id, _ = await _setup_with_db(
        mem_client, db_session, admin, headers, make_client_case, principal, member.email
    )
    h = expat_headers(member)
    routes = _expat_write_routes()
    assert len(routes) >= 8, routes  # the filter must find the real write surface

    for method, template in routes:
        url = _fill_path(template, case_id)
        # A document endpoint needs a multipart file to clear FastAPI parsing
        # BEFORE the manager's ownership check; the rest take a superset JSON
        # body ({value, body}) that satisfies every expat write schema.
        if method == "POST" and "document" in template:
            resp = await mem_client.request(
                method, url, headers=h, files={"file": ("a.pdf", b"x", "application/pdf")}
            )
        else:
            resp = await mem_client.request(
                method, url, headers=h, json={"value": None, "body": "x"}
            )
        # Every write path resolves the PRINCIPAL → a member is 404, never a
        # partial write. (404, not 403: a member must not even confirm existence
        # of a write endpoint — same non-revealing rule as the rest of the face.)
        assert resp.status_code == 404, (method, template, resp.status_code, resp.text)


# --- (2) a member sees no document outside their requirements (incl. step attachments) -


async def test_member_document_visibility_is_scoped_and_attachments_hidden(
    mem_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    principal = await make_expat_user(email="principal@x.io")
    member = await make_expat_user(email="member@x.io")
    case_id, _ = await _setup_with_db(
        mem_client, db_session, admin, headers, make_client_case, principal, member.email
    )
    # The agency uploads a document on the case (case-scoped, no person). The
    # member is attached to none of their requirements → it is invisible.
    up = await mem_client.post(
        f"/cases/{case_id}/documents",
        headers=headers,
        files={"file": ("passeport.pdf", b"secret", "application/pdf")},
    )
    assert up.status_code == 201, up.text
    doc_id = up.json()["id"]

    h = expat_headers(member)
    listing = await mem_client.get(f"/expat/cases/{case_id}/documents", headers=h)
    assert listing.status_code == 200, listing.text
    assert listing.json() == []  # not reachable via the member's requirements
    # And a direct download of that document is 404 for the member.
    assert (
        await mem_client.get(f"/expat/cases/{case_id}/documents/{doc_id}/download", headers=h)
    ).status_code == 404
    # No step attachment ever surfaces on the member's timeline.
    detail = (await mem_client.get(f"/expat/cases/{case_id}", headers=h)).json()
    assert all(step["attachments"] == [] for step in detail["timeline"])


# --- (4) a member of one case sees nothing of ANOTHER case (same agency) --------------


async def test_member_sees_nothing_of_another_case_same_agency(
    mem_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    principal = await make_expat_user(email="principal@x.io")
    member = await make_expat_user(email="member@x.io")
    case_a, _ = await _setup_with_db(
        mem_client, db_session, admin, headers, make_client_case, principal, member.email
    )
    # Another case of the SAME agency the member has nothing to do with.
    other_principal = await make_expat_user(email="other@x.io")
    case_b = await make_client_case(
        agency_id=admin.agency_id,
        principal_expat_user_id=other_principal.id,
        owner_agent_id=admin.id,
    )

    h = expat_headers(member)
    listing = await mem_client.get("/expat/cases", headers=h)
    assert {c["id"] for c in listing.json()} == {str(case_a)}  # only their case
    assert (await mem_client.get(f"/expat/cases/{case_b.id}", headers=h)).status_code == 404


# --- (5) cross-tenant: a member sees nothing of ANOTHER agency ------------------------


async def test_member_sees_nothing_cross_agency(
    mem_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    principal = await make_expat_user(email="principal@x.io")
    member = await make_expat_user(email="member@x.io")
    case_a, _ = await _setup_with_db(
        mem_client, db_session, admin, headers, make_client_case, principal, member.email
    )
    # A DIFFERENT agency with its own case — invisible to the member.
    other_admin = await make_agent(role=system_roles["admin"])
    foreign_principal = await make_expat_user(email="foreign@x.io")
    foreign_case = await make_client_case(
        agency_id=other_admin.agency_id, principal_expat_user_id=foreign_principal.id
    )

    h = expat_headers(member)
    listing = await mem_client.get("/expat/cases", headers=h)
    assert {c["id"] for c in listing.json()} == {str(case_a)}
    assert (await mem_client.get(f"/expat/cases/{foreign_case.id}", headers=h)).status_code == 404


# --- (6) get-or-create: an email already an expat_user (agency B) is reused -----------


async def test_get_or_create_reuses_the_pivot_across_agencies(
    mem_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    # In agency B, an expat is PRINCIPAL of a case (an existing login).
    admin_b = await make_agent(role=system_roles["admin"])
    shared = await make_expat_user(email="shared@x.io")
    case_b = await make_client_case(
        agency_id=admin_b.agency_id, principal_expat_user_id=shared.id, owner_agent_id=admin_b.id
    )
    # Agency A adds a MEMBER with the SAME email → the pivot is reused, no
    # second expat_user row.
    headers = agent_headers(admin)
    principal_a = await make_expat_user(email="principal-a@x.io")
    case_a, _ = await _setup_with_db(
        mem_client, db_session, admin, headers, make_client_case, principal_a, shared.email
    )

    # One and only one expat_user for that email.
    ids = (
        (await db_session.execute(select(ExpatUser.id).where(ExpatUser.email == "shared@x.io")))
        .scalars()
        .all()
    )
    assert ids == [shared.id]
    # The single login sees BOTH dossiers, each in its own context.
    listing = (await mem_client.get("/expat/cases", headers=expat_headers(shared))).json()
    assert {c["id"] for c in listing} == {str(case_a), str(case_b.id)}
    by_id = {c["id"]: c for c in listing}
    assert by_id[str(case_a)]["viewer_role"] == "member"  # member in agency A
    assert by_id[str(case_b.id)]["viewer_role"] == "principal"  # principal in agency B

    # EDITION leg (Arthur): agency C names the member WITHOUT email, then adds
    # the email by PATCH — the SAME pivot function, so STILL one expat_user
    # row, and the single login sees the third dossier too. No crossing.
    admin_c = await make_agent(role=system_roles["admin"])
    principal_c = await make_expat_user(email="principal-c@x.io")
    case_c = await make_client_case(
        agency_id=admin_c.agency_id,
        principal_expat_user_id=principal_c.id,
        owner_agent_id=admin_c.id,
    )
    hc = agent_headers(admin_c)
    created = await mem_client.post(
        f"/cases/{case_c.id}/persons",
        headers=hc,
        json={"full_name": "Marie Dupont", "relationship": "spouse"},
    )
    assert created.status_code == 201, created.text
    patched = await mem_client.patch(
        f"/cases/{case_c.id}/persons/{created.json()['id']}",
        headers=hc,
        json={"email": "shared@x.io"},
    )
    assert patched.status_code == 200, patched.text
    ids = (
        (await db_session.execute(select(ExpatUser.id).where(ExpatUser.email == "shared@x.io")))
        .scalars()
        .all()
    )
    assert ids == [shared.id]  # the edition reused the pivot — no second row
    listing = (await mem_client.get("/expat/cases", headers=expat_headers(shared))).json()
    by_id = {c["id"]: c for c in listing}
    assert set(by_id) == {str(case_a), str(case_b.id), str(case_c.id)}
    assert by_id[str(case_c.id)]["viewer_role"] == "member"


# --- Arthur: giving an email to an EXISTING member links the account ------------------


async def test_arthur_adding_email_to_existing_member_grants_access(
    mem_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
) -> None:
    """Arthur's case: the member was named at creation WITHOUT email (no
    account); the agency adds the email afterwards on the EDIT form → the
    account is linked (get-or-create, creation semantics) with read access."""
    headers = agent_headers(admin)
    principal = await make_expat_user(email="principal-arthur@x.io")
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=principal.id, owner_agent_id=admin.id
    )
    created = await mem_client.post(
        f"/cases/{case.id}/persons",
        headers=headers,
        json={"full_name": "Arthur Martin", "relationship": "son"},
    )
    assert created.status_code == 201, created.text
    person_id = created.json()["id"]
    row = await db_session.get(CasePerson, uuid.UUID(person_id))
    assert row is not None and row.expat_user_id is None  # named, no account

    patched = await mem_client.patch(
        f"/cases/{case.id}/persons/{person_id}", headers=headers, json={"email": "arthur@x.io"}
    )
    assert patched.status_code == 200, patched.text
    db_session.expire_all()
    row = await db_session.get(CasePerson, uuid.UUID(person_id))
    assert row is not None and row.expat_user_id is not None  # linked
    expat = await db_session.get(ExpatUser, row.expat_user_id)
    assert expat is not None and expat.email == "arthur@x.io"


async def test_email_change_on_an_already_linked_member_is_409(
    mem_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
) -> None:
    """A member who ALREADY has an access keeps it: a different email is an
    ACCESS TRANSFER disguised as a field edit → 409. Identical or empty
    email: clean no-op."""
    headers = agent_headers(admin)
    principal = await make_expat_user(email="principal-409@x.io")
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=principal.id, owner_agent_id=admin.id
    )
    created = await mem_client.post(
        f"/cases/{case.id}/persons",
        headers=headers,
        json={"full_name": "Marie Dupont", "relationship": "spouse", "email": "marie@x.io"},
    )
    assert created.status_code == 201, created.text
    person_id = created.json()["id"]
    linked_before = (await db_session.get(CasePerson, uuid.UUID(person_id))).expat_user_id
    assert linked_before is not None

    url = f"/cases/{case.id}/persons/{person_id}"
    denied = await mem_client.patch(url, headers=headers, json={"email": "autre@x.io"})
    assert denied.status_code == 409
    assert denied.json()["code"] == "person.email_change_forbidden"

    # Identical, empty and null emails: clean no-ops, the link never moves.
    for body in ({"email": "marie@x.io"}, {"email": ""}, {"email": None}):
        ok = await mem_client.patch(url, headers=headers, json=body)
        assert ok.status_code == 200, (body, ok.text)
    db_session.expire_all()
    assert (await db_session.get(CasePerson, uuid.UUID(person_id))).expat_user_id == linked_before


async def test_member_sees_the_timeline_after_the_email_edit(
    mem_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    """End of Arthur's path: once the email is added, the member's login sees
    the dossier's timeline (read-only member view)."""
    headers = agent_headers(admin)
    tid = (await mem_client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    await mem_client.post(f"/journeys/{tid}/steps", headers=headers, json={"name": "Collecte"})
    principal = await make_expat_user(email="principal-tl@x.io")
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=principal.id, owner_agent_id=admin.id
    )
    assign = await mem_client.post(
        f"/cases/{case.id}/journey", headers=headers, json={"journey_template_id": tid}
    )
    assert assign.status_code == 201, assign.text
    created = await mem_client.post(
        f"/cases/{case.id}/persons",
        headers=headers,
        json={"full_name": "Arthur Martin", "relationship": "son"},
    )
    person_id = created.json()["id"]
    patched = await mem_client.patch(
        f"/cases/{case.id}/persons/{person_id}",
        headers=headers,
        json={"email": "arthur-tl@x.io"},
    )
    assert patched.status_code == 200, patched.text

    member = (
        await db_session.execute(select(ExpatUser).where(ExpatUser.email == "arthur-tl@x.io"))
    ).scalar_one()
    # Arthur clicks his invitation link and activates (the PATCH sent the
    # same activation mail as creation) — simulated by stamping activated_at.
    member.activated_at = datetime.now(UTC)
    await db_session.commit()
    detail = await mem_client.get(f"/expat/cases/{case.id}", headers=expat_headers(member))
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["viewer_role"] == "member"
    assert [s["name"] for s in body["timeline"]] == ["Collecte"]


# --- non-regression: the member filter touches the EXPAT face ONLY -------------------
#
# documents_manager/documents_repository also serve the AGENT and the PROVIDER.
# The person-scoping lives on the expat read paths; the agent and external
# paths keep their own resolvers, untouched. One test per other face.


@pytest_asyncio.fixture
async def external_provider(make_agent: MakeAgent, admin: Agent, db_session: AsyncSession) -> Agent:
    role = (
        await db_session.execute(
            select(Role).where(Role.is_external.is_(True), Role.name == "external_lawyer")
        )
    ).scalar_one()
    return await make_agent(
        agency_id=admin.agency_id, role=role, is_external=True, email="lawyer@ext.com"
    )


async def test_agent_still_sees_all_case_documents(
    mem_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
) -> None:
    """The AGENT face is unaffected: an agent sees every document of the case,
    including one a member would never see."""
    headers = agent_headers(admin)
    principal = await make_expat_user(email="principal@x.io")
    member = await make_expat_user(email="member@x.io")
    case_id, _ = await _setup_with_db(
        mem_client, db_session, admin, headers, make_client_case, principal, member.email
    )
    up = await mem_client.post(
        f"/cases/{case_id}/documents",
        headers=headers,
        files={"file": ("passeport.pdf", b"secret", "application/pdf")},
    )
    assert up.status_code == 201, up.text
    docs = await mem_client.get(f"/cases/{case_id}/documents", headers=headers)
    assert docs.status_code == 200
    assert len(docs.json()) == 1  # the agent sees it — member scoping never applies here


async def test_provider_still_sees_case_documents(
    mem_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    external_provider: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
) -> None:
    """The PROVIDER (external) face is unaffected: an assigned provider sees the
    case's documents through its own assignment-scoped resolver, unchanged."""
    headers = agent_headers(admin)
    principal = await make_expat_user(email="principal@x.io")
    member = await make_expat_user(email="member@x.io")
    case_id, _ = await _setup_with_db(
        mem_client, db_session, admin, headers, make_client_case, principal, member.email
    )
    await mem_client.post(
        f"/cases/{case_id}/documents",
        headers=headers,
        files={"file": ("acte.pdf", b"data", "application/pdf")},
    )
    assign = await mem_client.post(
        f"/cases/{case_id}/external-assignments",
        headers=headers,
        json={"agent_id": str(external_provider.id)},
    )
    assert assign.status_code == 201, assign.text
    docs = await mem_client.get(
        f"/external/cases/{case_id}/documents", headers=agent_headers(external_provider)
    )
    assert docs.status_code == 200, docs.text
    assert len(docs.json()) == 1  # the provider sees it, exactly as before


async def _setup_with_db(
    client: AsyncClient,
    db: AsyncSession,
    admin: Agent,
    headers: dict,
    make_client_case: MakeClientCase,
    principal: ExpatUser,
    member_email: str,
) -> tuple[uuid.UUID, str]:
    """`_setup` with the principal-person passport fill wired to a real db."""
    tid = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    sid = (
        await client.post(
            f"/journeys/{tid}/steps",
            headers=headers,
            json={"name": "Collecte", "completion_mode": "agency_validation"},
        )
    ).json()["id"]
    await _add_field_req(client, headers, tid, sid, "passport_number", "principal")
    await _add_field_req(client, headers, tid, sid, "date_of_birth", "each_person")

    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=principal.id, owner_agent_id=admin.id
    )
    r = await client.post(
        f"/cases/{case.id}/persons",
        headers=headers,
        json={"full_name": "Marie Dupont", "relationship": "spouse", "email": member_email},
    )
    assert r.status_code == 201, r.text
    pid = (
        await client.post(
            f"/cases/{case.id}/journey", headers=headers, json={"journey_template_id": tid}
        )
    ).json()[0]["id"]
    await client.patch(
        f"/cases/{case.id}/steps/{pid}", headers=headers, json={"status": "in_progress"}
    )
    principal_person = await _principal_person_id(db, case.id)
    await client.patch(
        f"/cases/{case.id}/persons/{principal_person}",
        headers=headers,
        json={"passport_number": "X-SECRET-42"},
    )
    return case.id, tid
