"""ClientCase hub battery: create flow (link-or-create principal +
invitation always), filters/pagination ported from Prism, ActivityLog
atomicity, confidential notes, PDF export, tenant scoping."""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.activity import ActivityLog
from shared.models.agent import Agent
from shared.models.expat_user import ExpatUser
from shared.models.invitation import CaseInvitation
from shared.models.rbac import Role
from src.core import email
from src.core.config import get_settings
from tests.plugins.agency_plugin import MakeAgency
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeCaseNote, MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser


@pytest.fixture
def cases_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def member(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["member"])


@pytest_asyncio.fixture
async def admin(member: Agent, make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    """Admin of the SAME agency as `member`."""
    return await make_agent(agency_id=member.agency_id, role=system_roles["admin"])


def _payload(email_addr: str = "client@example.com", **overrides: object) -> dict[str, object]:
    return {
        "first_name": "Jean",
        "last_name": "Martin",
        "email": email_addr,
        "origin_country": "FR",
        "dest_country": "PY",
        **overrides,
    }


async def _activity_types(db: AsyncSession, case_id: str) -> list[str]:
    stmt = select(ActivityLog.action_type).where(ActivityLog.case_id == uuid.UUID(case_id))
    return list((await db.execute(stmt)).scalars())


# --- create -----------------------------------------------------------------------


async def test_create_case_new_expat_full_flow(
    cases_client: AsyncClient,
    db_session: AsyncSession,
    member: Agent,
    agent_headers: AuthHeaders,
) -> None:
    response = await cases_client.post(
        "/cases", headers=agent_headers(member), json=_payload("new@example.com")
    )
    assert response.status_code == 201
    body = response.json()
    # Owner defaults to the creator.
    assert body["owner_agent_id"] == str(member.id)

    expat = (
        await db_session.execute(select(ExpatUser).where(ExpatUser.email == "new@example.com"))
    ).scalar_one()
    assert expat.activated_at is None
    assert body["principal_expat_user_id"] == str(expat.id)

    invitation = (
        await db_session.execute(
            select(CaseInvitation).where(CaseInvitation.case_id == uuid.UUID(body["id"]))
        )
    ).scalar_one()
    assert invitation.status == "pending"

    assert len(email.outbox) == 1
    sent = email.outbox[0]
    activation_link = f"{get_settings().frontend_url}/space/activate/{invitation.token}"
    assert activation_link in sent.body  # text fallback
    assert sent.html is not None and activation_link in sent.html

    types = await _activity_types(db_session, body["id"])
    assert types == ["case.created", "case.invitation_sent"]


async def test_create_case_existing_activated_expat(
    cases_client: AsyncClient,
    db_session: AsyncSession,
    member: Agent,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
) -> None:
    existing = await make_expat_user(
        email="veteran@example.com", first_name="Vera", last_name="Original"
    )
    response = await cases_client.post(
        "/cases",
        headers=agent_headers(member),
        json=_payload("veteran@example.com", first_name="Wrong", last_name="Payload"),
    )
    assert response.status_code == 201
    body = response.json()
    # Linked, not duplicated…
    assert body["principal_expat_user_id"] == str(existing.id)
    count = len(
        (
            await db_session.execute(
                select(ExpatUser).where(ExpatUser.email == "veteran@example.com")
            )
        )
        .scalars()
        .all()
    )
    assert count == 1
    # …identity NOT overwritten by the payload…
    await db_session.refresh(existing)
    assert (existing.first_name, existing.last_name) == ("Vera", "Original")
    # …and invitation + mail still go out ("a new case awaits you").
    invitation = (
        await db_session.execute(
            select(CaseInvitation).where(CaseInvitation.case_id == uuid.UUID(body["id"]))
        )
    ).scalar_one()
    assert invitation.status == "pending"
    assert len(email.outbox) == 1
    sent = email.outbox[0]
    assert "nouveau dossier" in sent.subject.lower()
    login_link = f"{get_settings().frontend_url}/space/login"
    assert login_link in sent.body
    assert sent.html is not None and login_link in sent.html


async def test_create_case_owner_not_in_agency_422(
    cases_client: AsyncClient,
    member: Agent,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
) -> None:
    foreign_agent = await make_agent()  # other agency
    response = await cases_client.post(
        "/cases",
        headers=agent_headers(member),
        json=_payload(owner_agent_id=str(foreign_agent.id)),
    )
    assert response.status_code == 422


async def test_create_case_requires_case_edit(
    cases_client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    viewer = await make_agent(role=system_roles["viewer"])
    response = await cases_client.post("/cases", headers=agent_headers(viewer), json=_payload())
    assert response.status_code == 403


# --- list: pagination + filters -------------------------------------------------------


async def test_list_items_carry_principal_identity(
    cases_client: AsyncClient,
    member: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
) -> None:
    martin = await make_expat_user(
        first_name="Jean",
        last_name="Martin",
        email="jean.martin@example.com",
        preferred_lang="fr",
    )
    case = await make_client_case(agency_id=member.agency_id, principal_expat_user_id=martin.id)
    response = await cases_client.get("/cases", headers=agent_headers(member))
    assert response.status_code == 200
    items = response.json()["items"]
    item = next(c for c in items if c["id"] == str(case.id))
    assert item["principal"] == {
        "first_name": "Jean",
        "last_name": "Martin",
        "email": "jean.martin@example.com",
        "preferred_lang": "fr",
    }
    # The rest of the list contract is untouched.
    assert item["principal_expat_user_id"] == str(martin.id)
    assert "status" in item and "tags" in item


async def test_list_pagination_stable_no_overlap(
    cases_client: AsyncClient,
    member: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    for _ in range(3):
        await make_client_case(agency_id=member.agency_id)
    headers = agent_headers(member)
    page1 = (await cases_client.get("/cases?page=1&page_size=2", headers=headers)).json()
    page2 = (await cases_client.get("/cases?page=2&page_size=2", headers=headers)).json()
    assert page1["total"] == page2["total"] == 3
    ids1 = {c["id"] for c in page1["items"]}
    ids2 = {c["id"] for c in page2["items"]}
    assert len(ids1) == 2 and len(ids2) == 1
    assert ids1.isdisjoint(ids2)  # the id tiebreaker at work


async def test_list_filters(
    cases_client: AsyncClient,
    member: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(member)
    ru_expat = await make_expat_user(preferred_lang="ru")
    case_a = await make_client_case(
        agency_id=member.agency_id,
        status="in_progress",
        dest_country="PY",
        owner_agent_id=member.id,
        principal_expat_user_id=ru_expat.id,
        tags=["vip", "urgent"],
    )
    await make_client_case(agency_id=member.agency_id, status="prospect", dest_country="BG")

    async def ids(query: str) -> set[str]:
        response = await cases_client.get(f"/cases?{query}", headers=headers)
        assert response.status_code == 200
        return {c["id"] for c in response.json()["items"]}

    assert await ids("status=in_progress&status=validated") == {str(case_a.id)}
    assert await ids("dest_country=PY") == {str(case_a.id)}
    assert await ids(f"owner_agent_id={member.id}") == {str(case_a.id)}
    assert await ids("preferred_lang=ru") == {str(case_a.id)}


async def test_list_filter_tags_contains_all(
    cases_client: AsyncClient,
    member: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    both = await make_client_case(agency_id=member.agency_id, tags=["vip", "urgent"])
    await make_client_case(agency_id=member.agency_id, tags=["vip"])
    response = await cases_client.get("/cases?tag=vip&tag=urgent", headers=agent_headers(member))
    assert {c["id"] for c in response.json()["items"]} == {str(both.id)}


async def test_list_search_q_on_principal(
    cases_client: AsyncClient,
    member: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
) -> None:
    target = await make_expat_user(
        first_name="Aleksei", last_name="Volkov", email="volkov@example.com"
    )
    hit = await make_client_case(agency_id=member.agency_id, principal_expat_user_id=target.id)
    await make_client_case(agency_id=member.agency_id)

    by_name = await cases_client.get("/cases?q=volk", headers=agent_headers(member))
    assert {c["id"] for c in by_name.json()["items"]} == {str(hit.id)}
    by_email = await cases_client.get("/cases?q=volkov@example.com", headers=agent_headers(member))
    assert {c["id"] for c in by_email.json()["items"]} == {str(hit.id)}


async def test_list_scoped_to_agency(
    cases_client: AsyncClient,
    member: Agent,
    make_client_case: MakeClientCase,
    make_agency: MakeAgency,
    agent_headers: AuthHeaders,
) -> None:
    mine = await make_client_case(agency_id=member.agency_id)
    other_agency = await make_agency()
    await make_client_case(agency_id=other_agency.id)
    response = await cases_client.get("/cases", headers=agent_headers(member))
    assert {c["id"] for c in response.json()["items"]} == {str(mine.id)}


# --- detail ------------------------------------------------------------------------------


async def test_get_detail_with_relations(
    cases_client: AsyncClient,
    member: Agent,
    make_client_case: MakeClientCase,
    make_case_person: object,
    make_external_contact: object,
    make_case_note: MakeCaseNote,
    agent_headers: AuthHeaders,
) -> None:
    case = await make_client_case(agency_id=member.agency_id)
    await make_case_person(case=case, full_name="Lea Martin")  # type: ignore[operator]
    await make_external_contact(case=case, name="Maitre Robert")  # type: ignore[operator]
    await make_case_note(case=case, body="visible", author_agent_id=member.id)
    response = await cases_client.get(f"/cases/{case.id}", headers=agent_headers(member))
    assert response.status_code == 200
    body = response.json()
    # Unified persons list: principal (kind=principal) leads, then family.
    persons = body["persons"]
    principal = next(p for p in persons if p["kind"] == "principal")
    assert principal["id"] == body["principal_person_id"]
    assert principal["activated"] is False
    assert principal["full_name"] is None and principal["first_name"]  # identity resolved
    family = [p for p in persons if p["kind"] == "family"]
    assert [p["full_name"] for p in family] == ["Lea Martin"]
    assert [c["name"] for c in body["external_contacts"]] == ["Maitre Robert"]
    assert [n["body"] for n in body["notes"]] == ["visible"]


async def test_get_foreign_case_404(
    cases_client: AsyncClient,
    member: Agent,
    make_client_case: MakeClientCase,
    make_agency: MakeAgency,
    agent_headers: AuthHeaders,
) -> None:
    other_agency = await make_agency()
    foreign = await make_client_case(agency_id=other_agency.id)
    response = await cases_client.get(f"/cases/{foreign.id}", headers=agent_headers(member))
    assert response.status_code == 404


# --- patch + activity log -----------------------------------------------------------------


async def test_patch_status_logs_old_new(
    cases_client: AsyncClient,
    db_session: AsyncSession,
    member: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    case = await make_client_case(agency_id=member.agency_id, status="prospect")
    response = await cases_client.patch(
        f"/cases/{case.id}", headers=agent_headers(member), json={"status": "in_progress"}
    )
    assert response.status_code == 200
    row = (
        await db_session.execute(
            select(ActivityLog).where(
                ActivityLog.case_id == case.id,
                ActivityLog.action_type == "case.status_changed",
            )
        )
    ).scalar_one()
    assert row.details == {"old": "prospect", "new": "in_progress"}
    assert row.actor_id == member.id


async def test_patch_owner_logs_and_validates(
    cases_client: AsyncClient,
    db_session: AsyncSession,
    member: Agent,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    case = await make_client_case(agency_id=member.agency_id, owner_agent_id=member.id)
    response = await cases_client.patch(
        f"/cases/{case.id}",
        headers=agent_headers(member),
        json={"owner_agent_id": str(admin.id)},
    )
    assert response.status_code == 200
    row = (
        await db_session.execute(
            select(ActivityLog).where(
                ActivityLog.case_id == case.id,
                ActivityLog.action_type == "case.owner_changed",
            )
        )
    ).scalar_one()
    assert row.details == {"old": str(member.id), "new": str(admin.id)}


async def test_patch_other_fields_logs_case_updated(
    cases_client: AsyncClient,
    db_session: AsyncSession,
    member: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    case = await make_client_case(agency_id=member.agency_id, dest_country="PY", tags=[])
    response = await cases_client.patch(
        f"/cases/{case.id}",
        headers=agent_headers(member),
        json={"dest_country": "BG", "tags": ["vip"]},
    )
    assert response.status_code == 200
    row = (
        await db_session.execute(
            select(ActivityLog).where(
                ActivityLog.case_id == case.id, ActivityLog.action_type == "case.updated"
            )
        )
    ).scalar_one()
    assert row.details["changes"]["dest_country"] == {"old": "PY", "new": "BG"}
    assert row.details["changes"]["tags"] == {"old": [], "new": ["vip"]}


async def test_patch_with_expat_token_401(
    cases_client: AsyncClient,
    member: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    expat_headers: AuthHeaders,
) -> None:
    case = await make_client_case(agency_id=member.agency_id)
    expat = await make_expat_user()
    response = await cases_client.patch(
        f"/cases/{case.id}", headers=expat_headers(expat), json={"status": "closed"}
    )
    assert response.status_code == 401


# --- persons + externals ----------------------------------------------------------------------


async def test_person_crud_with_civil_status_and_activity(
    cases_client: AsyncClient,
    db_session: AsyncSession,
    member: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(member)
    case = await make_client_case(agency_id=member.agency_id)
    # Add a FAMILY member with civil status in one shot.
    created = await cases_client.post(
        f"/cases/{case.id}/persons",
        headers=headers,
        json={
            "full_name": "Lea Martin",
            "relationship": "spouse",
            "passport_number": "12AB34567",
            "date_of_birth": "1990-05-12",
            "nationality": "French",
            "sex": "F",
            "marital_status": "married",
        },
    )
    assert created.status_code == 201
    body = created.json()
    person_id = body["id"]
    assert body["kind"] == "family"
    assert body["passport_number"] == "12AB34567"
    assert body["date_of_birth"] == "1990-05-12"
    assert body["sex"] == "F" and body["marital_status"] == "married"

    updated = await cases_client.patch(
        f"/cases/{case.id}/persons/{person_id}",
        headers=headers,
        json={"relationship": "child", "phone": "+33600000000"},
    )
    assert updated.status_code == 200
    assert updated.json()["relationship"] == "child"
    assert updated.json()["phone"] == "+33600000000"
    assert updated.json()["passport_number"] == "12AB34567"  # untouched field survives

    deleted = await cases_client.delete(f"/cases/{case.id}/persons/{person_id}", headers=headers)
    assert deleted.status_code == 200
    types = await _activity_types(db_session, str(case.id))
    assert types == ["person.added", "person.updated", "person.removed"]


async def test_principal_civil_status_editable_not_deletable(
    cases_client: AsyncClient,
    member: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(member)
    case = await make_client_case(agency_id=member.agency_id)
    detail = (await cases_client.get(f"/cases/{case.id}", headers=headers)).json()
    principal_id = detail["principal_person_id"]

    # Edit the principal's civil status (name stays on expat_user).
    edited = await cases_client.patch(
        f"/cases/{case.id}/persons/{principal_id}",
        headers=headers,
        json={"passport_number": "PRINCIPAL99", "nationality": "Russian", "full_name": "ignored"},
    )
    assert edited.status_code == 200
    assert edited.json()["passport_number"] == "PRINCIPAL99"
    assert edited.json()["full_name"] is None  # full_name ignored for principal
    assert edited.json()["first_name"]  # identity still resolved from expat_user

    # The principal is never deletable.
    denied = await cases_client.delete(f"/cases/{case.id}/persons/{principal_id}", headers=headers)
    assert denied.status_code == 422


async def test_addresses_on_patch_case(
    cases_client: AsyncClient,
    member: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(member)
    case = await make_client_case(agency_id=member.agency_id)
    response = await cases_client.patch(
        f"/cases/{case.id}",
        headers=headers,
        json={
            "origin_street": "12 rue de Paris",
            "origin_city": "Lyon",
            "origin_postal_code": "69001",
            "dest_street": "Av. España 100",
            "dest_city": "Asunción",
            "dest_postal_code": "001",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["origin_street"] == "12 rue de Paris" and body["origin_city"] == "Lyon"
    assert body["dest_city"] == "Asunción" and body["dest_postal_code"] == "001"
    # Country columns untouched (filters/views read them).
    assert body["origin_country"] == "FR" and body["dest_country"] == "PY"


async def test_external_contact_crud(
    cases_client: AsyncClient,
    member: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(member)
    case = await make_client_case(agency_id=member.agency_id)
    created = await cases_client.post(
        f"/cases/{case.id}/external-contacts",
        headers=headers,
        json={"name": "Maitre Robert", "type": "notary", "email": "robert@notaires.fr"},
    )
    assert created.status_code == 201
    contact_id = created.json()["id"]
    updated = await cases_client.patch(
        f"/cases/{case.id}/external-contacts/{contact_id}",
        headers=headers,
        json={"type": "lawyer"},
    )
    assert updated.json()["type"] == "lawyer"
    assert (
        await cases_client.delete(
            f"/cases/{case.id}/external-contacts/{contact_id}", headers=headers
        )
    ).status_code == 200


# --- notes ---------------------------------------------------------------------------------------


async def test_member_cannot_create_confidential_note(
    cases_client: AsyncClient,
    member: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    case = await make_client_case(agency_id=member.agency_id)
    ok = await cases_client.post(
        f"/cases/{case.id}/notes",
        headers=agent_headers(member),
        json={"body": "normal note"},
    )
    assert ok.status_code == 201
    confidential = await cases_client.post(
        f"/cases/{case.id}/notes",
        headers=agent_headers(member),
        json={"body": "secret", "is_confidential": True},
    )
    assert confidential.status_code == 403


async def test_confidential_notes_filtered_by_permission(
    cases_client: AsyncClient,
    member: Agent,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    case = await make_client_case(agency_id=member.agency_id)
    created = await cases_client.post(
        f"/cases/{case.id}/notes",
        headers=agent_headers(admin),
        json={"body": "confidential intel", "is_confidential": True},
    )
    assert created.status_code == 201
    await cases_client.post(
        f"/cases/{case.id}/notes", headers=agent_headers(member), json={"body": "public note"}
    )

    member_view = (
        await cases_client.get(f"/cases/{case.id}/notes", headers=agent_headers(member))
    ).json()
    assert [n["body"] for n in member_view] == ["public note"]
    admin_view = (
        await cases_client.get(f"/cases/{case.id}/notes", headers=agent_headers(admin))
    ).json()
    assert {n["body"] for n in admin_view} == {"confidential intel", "public note"}
    # Same filtering on the case detail.
    member_detail = (
        await cases_client.get(f"/cases/{case.id}", headers=agent_headers(member))
    ).json()
    assert [n["body"] for n in member_detail["notes"]] == ["public note"]


async def test_note_activity_details_never_contain_content(
    cases_client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    case = await make_client_case(agency_id=admin.agency_id)
    response = await cases_client.post(
        f"/cases/{case.id}/notes",
        headers=agent_headers(admin),
        json={"body": "TOP-SECRET-CONTENT", "is_confidential": True},
    )
    assert response.status_code == 201
    row = (
        await db_session.execute(
            select(ActivityLog).where(
                ActivityLog.case_id == case.id, ActivityLog.action_type == "note.added"
            )
        )
    ).scalar_one()
    assert row.details == {"note_id": response.json()["id"], "is_confidential": True}
    assert "TOP-SECRET-CONTENT" not in str(row.details)


async def test_note_edit_author_only(
    cases_client: AsyncClient,
    member: Agent,
    admin: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    case = await make_client_case(agency_id=member.agency_id)
    note = (
        await cases_client.post(
            f"/cases/{case.id}/notes", headers=agent_headers(member), json={"body": "mine"}
        )
    ).json()
    own_edit = await cases_client.patch(
        f"/cases/{case.id}/notes/{note['id']}",
        headers=agent_headers(member),
        json={"body": "mine, edited"},
    )
    assert own_edit.status_code == 200
    foreign_edit = await cases_client.patch(
        f"/cases/{case.id}/notes/{note['id']}",
        headers=agent_headers(admin),
        json={"body": "hijacked"},
    )
    assert foreign_edit.status_code == 403
    foreign_delete = await cases_client.delete(
        f"/cases/{case.id}/notes/{note['id']}", headers=agent_headers(admin)
    )
    assert foreign_delete.status_code == 403


# --- export ---------------------------------------------------------------------------------------


async def test_export_pdf(
    cases_client: AsyncClient,
    member: Agent,
    make_client_case: MakeClientCase,
    make_case_person: object,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(member)
    case = await make_client_case(agency_id=member.agency_id)
    # Civil status on a family member + addresses on the case → the PDF
    # builder must include them without crashing (latin-1 path covered).
    await make_case_person(  # type: ignore[operator]
        case=case, full_name="Lea Martin", passport_number="X99", nationality="French"
    )
    await cases_client.patch(
        f"/cases/{case.id}",
        headers=headers,
        json={"origin_street": "12 rue", "dest_city": "Asunción"},
    )
    response = await cases_client.get(f"/cases/{case.id}/export", headers=headers)
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")


# --- RGPD isolation: civil status is case-scoped, never on expat_user --------------


async def test_civil_status_isolated_across_agencies_sharing_an_expat(
    cases_client: AsyncClient,
    make_agent: MakeAgent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """Two agencies, one shared expat_user as principal of a case each.
    Agency A sets the principal's civil status — agency B must NOT see
    it (it lives on case_person scoped to A's case, never on the shared
    expat_user)."""
    shared_expat = await make_expat_user()
    agent_a = await make_agent(role=system_roles["case_manager"])
    agent_b = await make_agent(role=system_roles["case_manager"])  # other agency
    case_a = await make_client_case(
        agency_id=agent_a.agency_id, principal_expat_user_id=shared_expat.id
    )
    case_b = await make_client_case(
        agency_id=agent_b.agency_id, principal_expat_user_id=shared_expat.id
    )

    detail_a = (
        await cases_client.get(f"/cases/{case_a.id}", headers=agent_headers(agent_a))
    ).json()
    principal_a = detail_a["principal_person_id"]
    set_status = await cases_client.patch(
        f"/cases/{case_a.id}/persons/{principal_a}",
        headers=agent_headers(agent_a),
        json={"passport_number": "SECRET-A", "nationality": "French"},
    )
    assert set_status.status_code == 200

    # Agency B's view of the SAME expat: civil status is empty.
    detail_b = (
        await cases_client.get(f"/cases/{case_b.id}", headers=agent_headers(agent_b))
    ).json()
    principal_b = next(p for p in detail_b["persons"] if p["kind"] == "principal")
    assert principal_b["passport_number"] is None
    assert principal_b["nationality"] is None
    # Same human identity (shared expat_user), different case_person rows.
    assert principal_b["email"] == detail_a["persons"][0]["email"]
    assert principal_b["id"] != principal_a


# --- multi-sort (Prism parity) ---------------------------------------------------


async def test_sort_single_and_multi(
    cases_client: AsyncClient,
    member: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(member)
    a = await make_client_case(agency_id=member.agency_id, status="prospect", dest_country="BG")
    b = await make_client_case(agency_id=member.agency_id, status="validated", dest_country="PY")
    c = await make_client_case(agency_id=member.agency_id, status="in_progress", dest_country="PY")

    asc = await cases_client.get("/cases?sort_by=status&order=asc", headers=headers)
    assert [i["id"] for i in asc.json()["items"]] == [str(c.id), str(a.id), str(b.id)]

    multi = await cases_client.get(
        "/cases?sort_by=dest_country,status&order=asc,desc", headers=headers
    )
    assert [i["id"] for i in multi.json()["items"]] == [str(a.id), str(b.id), str(c.id)]


async def test_sort_by_principal_last_name(
    cases_client: AsyncClient,
    member: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
) -> None:
    zoe = await make_expat_user(last_name="Zima")
    abad = await make_expat_user(last_name="Abad")
    case_z = await make_client_case(agency_id=member.agency_id, principal_expat_user_id=zoe.id)
    case_a = await make_client_case(agency_id=member.agency_id, principal_expat_user_id=abad.id)
    response = await cases_client.get(
        "/cases?sort_by=principal_last_name&order=asc", headers=agent_headers(member)
    )
    assert [i["id"] for i in response.json()["items"]] == [str(case_a.id), str(case_z.id)]


async def test_sort_validation_422(
    cases_client: AsyncClient, member: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(member)
    unknown = await cases_client.get("/cases?sort_by=ghost&order=asc", headers=headers)
    assert unknown.status_code == 422
    assert "ghost" in unknown.json()["detail"]
    mismatch = await cases_client.get("/cases?sort_by=status,created_at&order=asc", headers=headers)
    assert mismatch.status_code == 422
    bad_dir = await cases_client.get("/cases?sort_by=status&order=sideways", headers=headers)
    assert bad_dir.status_code == 422


async def test_sort_pagination_stable(
    cases_client: AsyncClient,
    member: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    for _ in range(3):
        await make_client_case(agency_id=member.agency_id, status="prospect")
    headers = agent_headers(member)
    q = "/cases?sort_by=status&order=asc&page_size=2"
    page1 = (await cases_client.get(f"{q}&page=1", headers=headers)).json()
    page2 = (await cases_client.get(f"{q}&page=2", headers=headers)).json()
    ids1 = {c["id"] for c in page1["items"]}
    ids2 = {c["id"] for c in page2["items"]}
    assert len(ids1) == 2 and len(ids2) == 1 and ids1.isdisjoint(ids2)


# --- AdvancedFilters tree (Prism parity) ---------------------------------------------


def _tree(*conditions: dict, groups: list | None = None) -> str:
    import json as _json

    return _json.dumps({"conditions": list(conditions), "groups": groups or []})


async def _tree_ids(client: AsyncClient, headers: dict[str, str], tree: str) -> set[str]:
    from urllib.parse import quote

    response = await client.get(f"/cases?filters={quote(tree)}", headers=headers)
    assert response.status_code == 200, response.text
    return {c["id"] for c in response.json()["items"]}


async def test_advanced_filters_every_operator(
    cases_client: AsyncClient,
    member: Agent,
    db_session: AsyncSession,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(member)
    expat = await make_expat_user(email="aleksei.volkov@example.com", preferred_lang="ru")
    old = await make_client_case(
        agency_id=member.agency_id,
        status="prospect",
        source="referral",
        principal_expat_user_id=expat.id,
        tags=["vip"],
    )
    new = await make_client_case(
        agency_id=member.agency_id, status="in_progress", source=None, tags=[]
    )
    # Distinct created_at values for the date operators.
    from datetime import datetime

    t_old = datetime(2026, 1, 10, 12, 0, 0)
    t_new = datetime(2026, 6, 1, 12, 0, 0)
    from shared.models.client_case import ClientCase as CC

    await db_session.execute(
        update(CC).where(CC.id == old.id).values(created_at=t_old, updated_at=t_old)
    )
    await db_session.execute(
        update(CC).where(CC.id == new.id).values(created_at=t_new, updated_at=t_new)
    )
    await db_session.commit()

    oid, nid = str(old.id), str(new.id)

    # eq / neq
    assert await _tree_ids(
        cases_client, headers, _tree({"field": "status", "operator": "eq", "value": "prospect"})
    ) == {oid}
    assert await _tree_ids(
        cases_client, headers, _tree({"field": "status", "operator": "neq", "value": "prospect"})
    ) == {nid}
    # in / not_in
    assert await _tree_ids(
        cases_client,
        headers,
        _tree({"field": "status", "operator": "in", "value": ["prospect", "closed"]}),
    ) == {oid}
    assert await _tree_ids(
        cases_client,
        headers,
        _tree({"field": "status", "operator": "not_in", "value": ["prospect", "closed"]}),
    ) == {nid}
    # gt / gte / lt / lte on created_at (dates coerced from strings)
    assert await _tree_ids(
        cases_client,
        headers,
        _tree({"field": "created_at", "operator": "gt", "value": "2026-03-01"}),
    ) == {nid}
    assert await _tree_ids(
        cases_client,
        headers,
        _tree({"field": "created_at", "operator": "gte", "value": "2026-06-01T12:00:00"}),
    ) == {nid}
    assert await _tree_ids(
        cases_client,
        headers,
        _tree({"field": "created_at", "operator": "lt", "value": "2026-03-01"}),
    ) == {oid}
    assert await _tree_ids(
        cases_client,
        headers,
        _tree({"field": "created_at", "operator": "lte", "value": "2026-01-10T12:00:00"}),
    ) == {oid}
    # between on created_at (the date-picker pair)
    assert await _tree_ids(
        cases_client,
        headers,
        _tree(
            {"field": "created_at", "operator": "between", "value": ["2026-01-01", "2026-02-01"]}
        ),
    ) == {oid}
    # contains / not_contains on a joined principal field
    assert await _tree_ids(
        cases_client,
        headers,
        _tree({"field": "principal_email", "operator": "contains", "value": "volkov"}),
    ) == {oid}
    assert await _tree_ids(
        cases_client,
        headers,
        _tree({"field": "principal_email", "operator": "not_contains", "value": "volkov"}),
    ) == {nid}
    # is_empty / is_not_empty on a nullable column
    assert await _tree_ids(
        cases_client, headers, _tree({"field": "source", "operator": "is_empty"})
    ) == {nid}
    assert await _tree_ids(
        cases_client, headers, _tree({"field": "source", "operator": "is_not_empty"})
    ) == {oid}
    # tags: contains (ANY-of) + is_empty
    assert await _tree_ids(
        cases_client, headers, _tree({"field": "tags", "operator": "contains", "value": ["vip"]})
    ) == {oid}
    assert await _tree_ids(
        cases_client, headers, _tree({"field": "tags", "operator": "is_empty"})
    ) == {nid}


async def test_advanced_filters_is_empty_on_joined_field(
    cases_client: AsyncClient,
    member: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
) -> None:
    """is_empty on principal_preferred_lang: matches the case whose
    principal has an empty-string lang, not the 'fr' one."""
    blank = await make_expat_user(preferred_lang="")
    case_blank = await make_client_case(
        agency_id=member.agency_id, principal_expat_user_id=blank.id
    )
    await make_client_case(agency_id=member.agency_id)  # principal lang "fr"
    matched = await _tree_ids(
        cases_client,
        agent_headers(member),
        _tree({"field": "principal_preferred_lang", "operator": "is_empty"}),
    )
    assert matched == {str(case_blank.id)}


async def test_advanced_filters_or_group_and_param_combination(
    cases_client: AsyncClient,
    member: Agent,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(member)
    a = await make_client_case(agency_id=member.agency_id, status="prospect", dest_country="BG")
    b = await make_client_case(agency_id=member.agency_id, status="validated", dest_country="PY")
    await make_client_case(agency_id=member.agency_id, status="in_progress", dest_country="DE")

    or_tree = _tree(
        groups=[
            {
                "logic": "or",
                "conditions": [
                    {"field": "dest_country", "operator": "eq", "value": "BG"},
                    {"field": "status", "operator": "eq", "value": "validated"},
                ],
            }
        ]
    )
    assert await _tree_ids(cases_client, headers, or_tree) == {str(a.id), str(b.id)}

    # Tree AND legacy param compose: the status param narrows the OR group.
    from urllib.parse import quote

    combined = await cases_client.get(
        f"/cases?status=validated&filters={quote(or_tree)}", headers=headers
    )
    assert {c["id"] for c in combined.json()["items"]} == {str(b.id)}


async def test_advanced_filters_validation_422(
    cases_client: AsyncClient, member: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(member)
    from urllib.parse import quote

    bad_json = await cases_client.get("/cases?filters={not-json", headers=headers)
    assert bad_json.status_code == 422
    unknown_field = await cases_client.get(
        f"/cases?filters={quote(_tree({'field': 'ghost', 'operator': 'eq', 'value': 1}))}",
        headers=headers,
    )
    assert unknown_field.status_code == 422
    bad_date_tree = _tree({"field": "created_at", "operator": "gt", "value": "not-a-date"})
    bad_date = await cases_client.get(f"/cases?filters={quote(bad_date_tree)}", headers=headers)
    assert bad_date.status_code == 422
    between_tree = _tree({"field": "created_at", "operator": "between", "value": ["2026-01-01"]})
    bad_between = await cases_client.get(f"/cases?filters={quote(between_tree)}", headers=headers)
    assert bad_between.status_code == 422
