"""« Texte long » custom field type (lot 15/09/2026) — a textarea next to
the existing types, SAME mechanics everywhere (storage in the JSONB,
visibility, sections, permissions, export, import), plus ONE rule of its
own: a 5 000-character cap, refused with a stable code.

Covers, against the real app:
(a) a definition of this type is created like any other (no options, any
    scope), and the type is IMMUTABLE like the others;
(b) a value AT the cap is stored verbatim (newlines and spacing kept), on
    a case person AND on a client profile;
(c) one character past the cap → 422 `custom_field.long_text_too_long`
    with the key, the length and the cap in params — and the aggregate
    message still names every bad field of the PATCH;
(d) the agency CSV export writes the value as-is, newlines preserved
    under proper CSV quoting, and the same CSV imports back verbatim
    (the round trip), the cap holding on import too.
"""

import csv
import io
import zipfile

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.rbac import Role
from src.custom_fields.custom_fields_validation import LONG_TEXT_MAX_LENGTH, LONG_TEXT_TOO_LONG
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase

pytestmark = pytest.mark.usefixtures("rbac_baseline")

# A multi-line value with the shapes a textarea produces: blank lines,
# leading/trailing spaces on a line, a CSV-hostile line (comma + quotes).
NOTES = 'Ligne 1, avec virgule\n\n  Ligne 3 "citée" ; point-virgule\nDernière ligne'


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


async def _define(client: AsyncClient, headers: dict[str, str], **overrides: object) -> dict:
    payload = {
        "key": "notes_dossier",
        "label": "Notes du dossier",
        "field_type": "long_text",
        **overrides,
    }
    response = await client.post("/agencies/me/custom-fields", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _at_cap() -> str:
    """Exactly LONG_TEXT_MAX_LENGTH characters, newlines included."""
    line = "abcdefghij\n"  # 11 characters
    text = line * (LONG_TEXT_MAX_LENGTH // len(line))
    text += "x" * (LONG_TEXT_MAX_LENGTH - len(text))
    assert len(text) == LONG_TEXT_MAX_LENGTH
    return text


# --- (a) the definition ----------------------------------------------------------------


async def test_long_text_definition_is_created_like_any_type(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    created = await _define(client, headers, scope="person", profile_section="misc")
    assert created["field_type"] == "long_text"
    assert created["options"] is None and created["scope"] == "person"
    listing = (await client.get("/agencies/me/custom-fields", headers=headers)).json()
    assert [d["field_type"] for d in listing] == ["long_text"]
    # Type is immutable, like every type: the PATCH contract has no field_type.
    renamed = await client.patch(
        f"/agencies/me/custom-fields/{created['id']}",
        headers=headers,
        json={"label": "Notes", "field_type": "text"},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["field_type"] == "long_text"
    # A case-scoped one too (the default scope), no section required.
    case_scoped = await _define(client, headers, key="remarques", label="Remarques")
    assert case_scoped["scope"] == "case" and case_scoped["field_type"] == "long_text"


# --- (b) + (c) values: at the cap, verbatim; past the cap, the coded 422 ---------------


async def test_value_at_the_cap_is_kept_verbatim_and_one_more_is_refused_with_the_code(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
) -> None:
    headers = agent_headers(admin)
    await _define(client, headers)  # scope case
    await _define(client, headers, key="age", label="Âge", field_type="number")
    case = await make_client_case(agency_id=admin.agency_id)
    person_id = (await client.get(f"/cases/{case.id}", headers=headers)).json()[
        "principal_person_id"
    ]
    url = f"/cases/{case.id}/persons/{person_id}"

    # Verbatim: newlines, blank lines, inner spacing — nothing normalised.
    ok = await client.patch(url, headers=headers, json={"custom_fields": {"notes_dossier": NOTES}})
    assert ok.status_code == 200, ok.text
    assert ok.json()["custom_fields"]["notes_dossier"] == NOTES
    read = (await client.get(f"/cases/{case.id}", headers=headers)).json()
    principal = next(p for p in read["persons"] if p["id"] == person_id)
    assert principal["custom_fields"]["notes_dossier"] == NOTES

    at_cap = _at_cap()
    ok = await client.patch(url, headers=headers, json={"custom_fields": {"notes_dossier": at_cap}})
    assert ok.status_code == 200, ok.text
    assert ok.json()["custom_fields"]["notes_dossier"] == at_cap

    over = at_cap + "!"
    bad = await client.patch(url, headers=headers, json={"custom_fields": {"notes_dossier": over}})
    assert bad.status_code == 422, bad.text
    body = bad.json()
    assert body["code"] == LONG_TEXT_TOO_LONG == "custom_field.long_text_too_long"
    assert body["params"] == {
        "key": "notes_dossier",
        "length": LONG_TEXT_MAX_LENGTH + 1,
        "max_length": LONG_TEXT_MAX_LENGTH,
    }
    assert "Notes du dossier" in body["detail"]
    # The stored value did not move.
    read = (await client.get(f"/cases/{case.id}", headers=headers)).json()
    principal = next(p for p in read["persons"] if p["id"] == person_id)
    assert principal["custom_fields"]["notes_dossier"] == at_cap

    # Accumulation kept: a second bad field is still reported in the
    # message, the envelope code is the long-text one.
    both = await client.patch(
        url,
        headers=headers,
        json={"custom_fields": {"notes_dossier": over, "age": "not-a-number"}},
    )
    assert both.status_code == 422
    assert both.json()["code"] == LONG_TEXT_TOO_LONG
    assert "Âge" in both.json()["detail"] and "Notes du dossier" in both.json()["detail"]


async def test_long_text_holds_on_a_client_profile_too(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """Same validation path on the profile face (scope person)."""
    headers = agent_headers(admin)
    await _define(client, headers, scope="person", profile_section="misc")
    created = await client.post(
        "/client-profiles",
        headers=headers,
        json={"first_name": "Léa", "last_name": "Long", "email": "lea@example.com"},
    )
    assert created.status_code == 201, created.text
    profile_id = created.json()["id"]
    ok = await client.patch(
        f"/client-profiles/{profile_id}",
        headers=headers,
        json={"custom_fields": {"notes_dossier": NOTES}},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["custom_fields"]["notes_dossier"] == NOTES
    bad = await client.patch(
        f"/client-profiles/{profile_id}",
        headers=headers,
        json={"custom_fields": {"notes_dossier": "y" * (LONG_TEXT_MAX_LENGTH + 1)}},
    )
    assert bad.status_code == 422 and bad.json()["code"] == LONG_TEXT_TOO_LONG


# --- (d) export as-is, import back: the round trip ------------------------------------


def _read_zip(content: bytes) -> dict[str, str]:
    archive = zipfile.ZipFile(io.BytesIO(content))
    return {name: archive.read(name).decode("utf-8-sig") for name in archive.namelist()}


async def test_export_keeps_newlines_and_the_csv_imports_back_verbatim(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    await _define(client, headers, scope="person", profile_section="misc")
    created = await client.post(
        "/client-profiles",
        headers=headers,
        json={"first_name": "Marc", "last_name": "Export", "email": "marc@example.com"},
    )
    assert created.status_code == 201, created.text
    ok = await client.patch(
        f"/client-profiles/{created.json()['id']}",
        headers=headers,
        json={"custom_fields": {"notes_dossier": NOTES}},
    )
    assert ok.status_code == 200, ok.text

    # EXPORT: the cell is the value itself; the writer quotes it (the raw
    # file carries the quoted, doubled-quote form), so a CSV reader gets
    # the exact string back — newlines included.
    resp = await client.get("/agencies/me/export", headers=headers)
    assert resp.status_code == 200, resp.text
    persons_csv = _read_zip(resp.content)["fiches-personnes.csv"]
    quoted = '"Ligne 1, avec virgule\n\n  Ligne 3 ""citée"" ; point-virgule\nDernière ligne"'
    assert quoted in persons_csv  # RFC 4180: quoted cell, doubled quotes, raw newlines
    rows = list(csv.reader(io.StringIO(persons_csv)))
    header = rows[0]
    col = header.index("Notes du dossier")
    marc = next(r for r in rows[1:] if "marc@example.com" in r)
    assert marc[col] == NOTES  # exact, newlines preserved through the quoting

    # IMPORT the exported CSV back (mapping the export's own headers) on a
    # second profile: the value lands verbatim.
    round_trip = (
        "Prénom,Nom,Courriel,Notes du dossier\r\n"
        + ",".join(
            [
                "Rita",
                "Retour",
                "rita@example.com",
                '"' + NOTES.replace('"', '""') + '"',
            ]
        )
        + "\r\n"
    )
    imported = await client.post(
        "/imports/client-profiles",
        headers=headers,
        json={
            "csv_text": round_trip,
            "mapping": {
                "Prénom": "first_name",
                "Nom": "last_name",
                "Courriel": "email",
                "Notes du dossier": "notes_dossier",
            },
        },
    )
    assert imported.status_code == 200, imported.text
    report = imported.json()
    assert [c["email"] for c in report["created"]] == ["rita@example.com"]
    listing = (await client.get("/client-profiles?search=rita@", headers=headers)).json()
    rita_id = listing["items"][0]["id"]
    detail = (await client.get(f"/client-profiles/{rita_id}", headers=headers)).json()
    assert detail["custom_fields"]["notes_dossier"] == NOTES

    # The cap holds on import, the same way every type's bad cell does:
    # the PREVIEW names the column with `invalid_value`, the import keeps
    # the row and drops the value — never a truncation.
    too_long = (
        "Prénom,Nom,Courriel,Notes du dossier\r\n"
        f'Tom,Trop,tom@example.com,"{"z" * (LONG_TEXT_MAX_LENGTH + 1)}"\r\n'
    )
    mapping = {
        "Prénom": "first_name",
        "Nom": "last_name",
        "Courriel": "email",
        "Notes du dossier": "notes_dossier",
    }
    preview = await client.post(
        "/imports/client-profiles/preview",
        headers=headers,
        json={"csv_text": too_long, "mapping": mapping},
    )
    assert preview.status_code == 200, preview.text
    verdict = preview.json()["rows"][0]
    assert verdict["issues"] == [{"column": "Notes du dossier", "code": "invalid_value"}]
    assert "notes_dossier" not in verdict["person"]
    refused = await client.post(
        "/imports/client-profiles",
        headers=headers,
        json={"csv_text": too_long, "mapping": mapping},
    )
    assert refused.status_code == 200, refused.text
    tom_id = refused.json()["created"][0]["profile_id"]
    detail = (await client.get(f"/client-profiles/{tom_id}", headers=headers)).json()
    assert "notes_dossier" not in detail["custom_fields"]  # dropped, not truncated
