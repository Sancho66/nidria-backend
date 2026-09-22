"""Identity alternatives, ordered deduplication and no-email boundaries."""

import base64
import io

import openpyxl
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from src.core import email as email_module
from tests.plugins.agency_plugin import MakeAgency
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


@pytest.mark.parametrize("file_format", ["csv", "xlsx"])
async def test_identity_alternatives_blank_rows_and_counts(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    file_format: str,
) -> None:
    columns = ["Email", "First", "Last", "Phone"]
    records = [
        ["", "Alice", "", "+33 6 12 34 56 78"],
        ["", "Élodie", "Dupré", ""],
        ["", "", "", ""],
        ["", "Changed", "Name", "0033 (6) 12-34-56-78"],
        ["", "ELODIE", "DUPRE", ""],
        ["ONLY@example.com", "", "", ""],
        ["", "", "", "0611111111"],
        ["", "FirstOnly", "", ""],
    ]
    body = {
        "mapping": dict(zip(columns, ["email", "first_name", "last_name", "phone"], strict=True))
    }
    if file_format == "csv":
        body["csv_text"] = "\n".join(",".join(row) for row in [columns, *records]) + "\n"
    else:
        book = openpyxl.Workbook()
        sheet = book.active
        assert sheet is not None
        for row in [columns, *records]:
            sheet.append(row)
        buffer = io.BytesIO()
        book.save(buffer)
        body.update(filename="contacts.xlsx", file_b64=base64.b64encode(buffer.getvalue()).decode())
    headers = agent_headers(admin)
    preview = await client.post("/imports/client-profiles/preview", headers=headers, json=body)
    assert preview.status_code == 200, preview.text
    assert preview.json()["summary"] == {
        "create": 4,
        "link": 2,
        "ignore": 1,
        "ignore_reasons": {"missing_identity": 1},
        "create_with_email": 1,
        "create_without_email": 3,
    }
    assert (await db_session.execute(text("SELECT count(*) FROM client_profile"))).scalar_one() == 0
    result = await client.post("/imports/client-profiles", headers=headers, json=body)
    assert result.status_code == 200, result.text
    report = result.json()
    assert report["total_rows"] == 7
    assert report["created_count"] == 4
    assert report["created_without_email"] == 3
    assert report["created_with_email"] == 1
    assert [row["row"] for row in report["created"]] == [1, 2, 6, 7]
    assert [(row["row"], row["reason"]) for row in report["ignored"]] == [
        (8, "missing_identity"),
    ]
    assert [row["profile_id"] for row in report["linked"]] == [
        report["created"][0]["profile_id"],
        report["created"][1]["profile_id"],
    ]
    first = await client.get(
        f"/client-profiles/{report['created'][0]['profile_id']}", headers=headers
    )
    assert first.status_code == 200, first.text
    assert first.json()["first_name"] == "Alice"
    assert first.json()["last_name"] == "Name"  # Fill the gap without replacing Alice.
    assert first.json()["phone"] == "+33 6 12 34 56 78"
    assert first.json()["email"] == ""
    assert first.json()["expat_user_id"] is None
    assert first.json()["derived_status"] == "prospect"
    assert email_module.outbox == []
    again = await client.post("/imports/client-profiles", headers=headers, json=body)
    assert again.status_code == 200, again.text
    assert again.json()["created"] == []
    assert again.json()["created_count"] == 0
    assert len(again.json()["linked"]) == 6
    assert again.json()["created_without_email"] == 0


@pytest.mark.parametrize(
    ("csv_text", "mapping", "accepted"),
    [
        ("First,Phone\nAlice,06 12 34 56 78\n", {"First": "first_name", "Phone": "phone"}, True),
        ("First,Last\nAlice,Martin\n", {"First": "first_name", "Last": "last_name"}, True),
        ("Phone\n06.12.34.56.78\n", {"Phone": "phone"}, True),
        ("Email\nonly@example.com\n", {"Email": "email"}, True),
        ("First,Phone\nAlice,0612345678\n", {"First": "first_name"}, False),
        ("First,Last\nAlice,Martin\n", {"Last": "last_name"}, False),
        ("Phone\n---\n", {"Phone": "phone"}, False),
    ],
)
async def test_only_mapped_identifiers_count(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    csv_text: str,
    mapping: dict[str, str],
    accepted: bool,
) -> None:
    body = {"csv_text": csv_text, "mapping": mapping}
    for endpoint in ("/imports/client-profiles/preview", "/imports/client-profiles"):
        response = await client.post(endpoint, headers=agent_headers(admin), json=body)
        assert response.status_code == 200, response.text
        if endpoint.endswith("preview"):
            assert response.json()["rows"][0]["status"] == ("create" if accepted else "ignore")
        else:
            assert len(response.json()["created"]) == int(accepted)
            if not accepted:
                assert response.json()["ignored"][0]["reason"] == "missing_identity"
                assert response.json()["ignored"][0]["row"] == 1


async def test_fallback_dedup_is_agency_scoped_and_email_phone_take_precedence(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_agent: MakeAgent,
    make_agency: MakeAgency,
    system_roles: dict[str, Role],
) -> None:
    foreign = await make_agent(agency_id=(await make_agency()).id, role=system_roles["admin"])
    body = {
        "csv_text": "Email,First,Last,Phone\na@example.com,Élodie,Dupré,+33 6 00 00 00 01\n",
        "mapping": {"Email": "email", "First": "first_name", "Last": "last_name", "Phone": "phone"},
    }
    foreign_result = await client.post(
        "/imports/client-profiles", headers=agent_headers(foreign), json=body
    )
    assert foreign_result.status_code == 200, foreign_result.text
    headers = agent_headers(admin)
    foreign_only = await client.post(
        "/imports/client-profiles/preview",
        headers=headers,
        json={
            **body,
            "csv_text": "Email,First,Last,Phone\n,Other,Name,0033 6 00 00 00 01\n,ELODIE,DUPRE,\n",
        },
    )
    assert foreign_only.status_code == 200, foreign_only.text
    assert [row["status"] for row in foreign_only.json()["rows"]] == ["create", "create"]
    initial = await client.post("/imports/client-profiles", headers=headers, json=body)
    assert initial.status_code == 200, initial.text
    profile_id = initial.json()["created"][0]["profile_id"]
    body["csv_text"] = (
        "Email,First,Last,Phone\n"
        ",Changed,Name,0033 6 00 00 00 01\n"  # Phone matches a profile with email.
        ",ELODIE,DUPRE,\n"  # Full name matches the same agency profile.
        "b@example.com,Élodie,Dupré,+33 6 00 00 00 01\n"  # Email takes precedence.
        ",Élodie,Dupré,0600000002\n"  # Phone takes precedence over the same name.
        ",Élodie,Du pré,\n"  # Internal spacing remains exact.
    )
    result = await client.post("/imports/client-profiles", headers=headers, json=body)
    assert result.status_code == 200, result.text
    assert [row["profile_id"] for row in result.json()["linked"]] == [profile_id, profile_id]
    assert len(result.json()["created"]) == 3
    assert result.json()["created_without_email"] == 2


async def test_no_email_profile_refuses_client_access_and_cannot_receive_reminders(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    imported = await client.post(
        "/imports/client-profiles",
        headers=headers,
        json={
            "csv_text": "First,Phone\nAlice,0612345678\n",
            "mapping": {"First": "first_name", "Phone": "phone"},
        },
    )
    assert imported.status_code == 200, imported.text
    profile_id = imported.json()["created"][0]["profile_id"]
    invitation = await client.post(f"/client-profiles/{profile_id}/cases", headers=headers, json={})
    assert invitation.status_code == 422, invitation.text
    assert invitation.json()["code"] == "profile.no_email"
    reminder = await client.post(
        f"/cases/{profile_id}/reminders",
        headers=headers,
        json={
            "channel": "mail",
            "recipient_type": "expat",
            "message_body": "Hello",
            "scheduled_at": "2027-01-01T10:00:00Z",
        },
    )
    assert reminder.status_code == 404, reminder.text
    assert reminder.json()["code"] == "not_found"
    for table in ("expat_user", "client_case", "case_invitation", "reminder"):
        assert (await db_session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one() == 0
    assert email_module.outbox == []
    # Once email is added, incomplete names still produce a stable refusal, never a 500.
    patched = await client.patch(
        f"/client-profiles/{profile_id}", headers=headers, json={"email": "alice@example.com"}
    )
    assert patched.status_code == 200, patched.text
    incomplete = await client.post(f"/client-profiles/{profile_id}/cases", headers=headers, json={})
    assert incomplete.status_code == 422, incomplete.text
    assert incomplete.json()["code"] == "profile.missing_identity"
    assert email_module.outbox == []


async def test_profile_identity_import_retains_authorization_guards(
    client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
    expat_user: ExpatUser,
    expat_headers: AuthHeaders,
) -> None:
    viewer = await make_agent(role=system_roles["viewer"])
    body = {"csv_text": "Phone\n0612345678\n", "mapping": {"Phone": "phone"}}
    for endpoint in ("/imports/client-profiles", "/imports/client-profiles/preview"):
        for headers, expected in (
            ({}, 401),
            (agent_headers(viewer), 403),
            (expat_headers(expat_user), 401),
        ):
            response = await client.post(endpoint, headers=headers, json=body)
            assert response.status_code == expected, response.text


async def test_invalid_identifiers_and_unmapped_corrections_cannot_bypass_identity(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    body = {
        "csv_text": "Email,Phone,First,Last\ninvalid,0612345678,,\ninvalid,,,\n",
        "mapping": {"Email": "email", "Phone": "phone", "First": "first_name", "Last": "last_name"},
    }
    preview = await client.post("/imports/client-profiles/preview", headers=headers, json=body)
    assert preview.status_code == 200, preview.text
    rows = preview.json()["rows"]
    assert [row["status"] for row in rows] == ["create", "ignore"]
    assert rows[1]["reason"] == "missing_identity"
    assert [{"column": issue["column"], "code": issue["code"]} for issue in rows[0]["issues"]] == [
        {"column": "Email", "code": "invalid_value"}
    ]
    imported = await client.post("/imports/client-profiles", headers=headers, json=body)
    assert imported.status_code == 200, imported.text
    assert imported.json()["created_without_email"] == 1
    assert imported.json()["created"][0]["email"] is None
    corrected = await client.post(
        "/imports/client-profiles/preview",
        headers=headers,
        json={
            "csv_text": "First\nAlice\n",
            "mapping": {"First": "first_name"},
            "corrections": [{"row_index": 1, "target": "email", "value": "alice@example.com"}],
        },
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["rows"][0]["reason"] == "missing_identity"
    assert [
        {"column": issue["column"], "code": issue["code"]}
        for issue in corrected.json()["rows"][0]["issues"]
    ] == [{"column": "(correction)", "code": "unmapped_identifier"}]


async def test_same_file_dedup_uses_selected_key_without_falling_back(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    response = await client.post(
        "/imports/client-profiles",
        headers=agent_headers(admin),
        json={
            "csv_text": (
                "Email,Phone,First,Last\n"
                "one@example.com,0612345678,Alice,Martin\n"
                "two@example.com,0612345678,Alice,Martin\n"
                ",0612345678,Alice,Martin\n"
                ",0712345678,Alice,Martin\n"
                ",,Alice,Martin\n"
                ",,ALICE,MARTIN\n"
            ),
            "mapping": {
                "Email": "email",
                "Phone": "phone",
                "First": "first_name",
                "Last": "last_name",
            },
        },
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["created"]) == 3
    assert len(response.json()["linked"]) == 3
    assert {row["profile_id"] for row in response.json()["linked"]} == {
        response.json()["created"][0]["profile_id"]
    }
    assert response.json()["created_without_email"] == 1


async def test_later_rows_can_use_identity_gaps_completed_by_a_duplicate(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    body = {
        "csv_text": "Phone,First,Last\n0612345678,,\n06 12 34 56 78,Alice,Martin\n,ALICE,MARTIN\n",
        "mapping": {"Phone": "phone", "First": "first_name", "Last": "last_name"},
    }
    headers = agent_headers(admin)
    preview = await client.post("/imports/client-profiles/preview", headers=headers, json=body)
    assert preview.status_code == 200, preview.text
    assert [row["status"] for row in preview.json()["rows"]] == ["create", "link", "link"]
    result = await client.post("/imports/client-profiles", headers=headers, json=body)
    assert result.status_code == 200, result.text
    assert len(result.json()["created"]) == 1
    assert len(result.json()["linked"]) == 2
    assert {row["profile_id"] for row in result.json()["linked"]} == {
        result.json()["created"][0]["profile_id"]
    }


@pytest.mark.parametrize("target", ["email", "phone", "first_name", "last_name"])
async def test_custom_field_creation_cannot_supply_an_unmapped_identifier(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    target: str,
) -> None:
    body = {
        "csv_text": "First,Contact\nAlice,alice@example.com\n",
        "mapping": {"First": "first_name"},
        "create_fields": [{"column": "Contact", "label": target, "kind": "text"}],
    }
    for endpoint in ("/imports/client-profiles/preview", "/imports/client-profiles"):
        response = await client.post(endpoint, headers=agent_headers(admin), json=body)
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "import.create_field_conflict"
        assert response.json()["params"] == {"column": "Contact", "target": target}


async def test_custom_definition_cannot_override_native_identity_validation(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    from shared.models.custom_field import CustomFieldDefinition

    db_session.add(
        CustomFieldDefinition(
            agency_id=admin.agency_id,
            key="email",
            label="Legacy number",
            field_type="number",
            scope="person",
        )
    )
    await db_session.commit()
    headers = agent_headers(admin)
    body = {"csv_text": "Email\n123\nvalid@example.com\n", "mapping": {"Email": "email"}}
    preview = await client.post("/imports/client-profiles/preview", headers=headers, json=body)
    assert preview.status_code == 200, preview.text
    assert [row["status"] for row in preview.json()["rows"]] == ["ignore", "create"]
    result = await client.post("/imports/client-profiles", headers=headers, json=body)
    assert result.status_code == 200, result.text
    assert result.json()["created"][0]["email"] == "valid@example.com"
    assert result.json()["ignored"][0]["reason"] == "missing_identity"
    # Creating a field through a different label must not bypass its resolved key.
    conflict = await client.post(
        "/imports/client-profiles/preview",
        headers=headers,
        json={
            "csv_text": "First,Contact\nAlice,123\n",
            "mapping": {"First": "first_name"},
            "create_fields": [{"column": "Contact", "label": "Legacy number", "kind": "number"}],
        },
    )
    assert conflict.status_code == 422, conflict.text
    assert conflict.json()["code"] == "import.create_field_conflict"
