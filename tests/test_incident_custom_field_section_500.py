"""Prod incident 15/09/2026 18:03:10 UTC — Fly-Request-Id 01M2K3SCZH4GCRY2C6V2KM5PPK.

POST /agencies/me/custom-fields answered 500, empty text/plain, for a
superadmin acting inside an agency: type text, label « Date dernier
échange » (fr), section « Suivi Client / Prospect » (key
`suivi_client_prospect`, 21 chars), key `date_dernier_echange`, not
required. Cause: `custom_field_definition.profile_section` was String(20)
while a section key may be 50 (asyncpg « value too long for type character
varying(20) »). Two witnesses:

(a) the EXACT request, section created through the product route, admin
    AND superadmin → 201, the definition sits in its section, listed under
    it; the company surface too (same column width, same fix);
(b) the second defect: an unhandled exception must never leave as a bare
    text/plain 500 — the JSON envelope, the stable code `internal_error`,
    the request id (Fly's header echoed, minted otherwise) in params and in
    `X-Request-Id`, the full traceback logged with that id.
"""

import logging
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from shared.models.agent import Agent
from shared.models.rbac import Role
from src.agencies.agencies_manager import AgenciesManager
from src.main import app
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent

pytestmark = pytest.mark.usefixtures("rbac_baseline")

SECTION_KEY = "suivi_client_prospect"  # 21 characters — the prod section
INCIDENT_PAYLOAD: dict[str, Any] = {
    "key": "date_dernier_echange",
    "label": "Date dernier échange",
    "label_i18n": {"fr": "Date dernier échange"},
    "field_type": "text",
    "required": False,
    "scope": "person",
    "profile_section": SECTION_KEY,
}


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


@pytest_asyncio.fixture
async def superadmin(admin: Agent, make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    """A superadmin acting INSIDE the admin's agency — the prod actor."""
    return await make_agent(agency_id=admin.agency_id, role=system_roles["superadmin"])


async def _create_section(client: AsyncClient, headers: dict[str, str], surface: str) -> None:
    response = await client.post(
        "/agencies/me/profile-sections",
        headers=headers,
        json={
            "surface": surface,
            "key": SECTION_KEY,
            "label_i18n": {"fr": "Suivi Client / Prospect"},
        },
    )
    assert response.status_code == 201, response.text


# --- (a) the exact incident request, now 201 -----------------------------------------


@pytest.mark.parametrize("actor", ["superadmin", "admin"])
async def test_incident_payload_creates_the_field_in_a_21_char_section(
    client: AsyncClient,
    admin: Agent,
    superadmin: Agent,
    agent_headers: AuthHeaders,
    actor: str,
) -> None:
    assert len(SECTION_KEY) == 21
    headers = agent_headers(superadmin if actor == "superadmin" else admin)
    await _create_section(client, headers, "person")

    created = await client.post(
        "/agencies/me/custom-fields", headers=headers, json=INCIDENT_PAYLOAD
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["profile_section"] == SECTION_KEY
    assert body["field_type"] == "text" and body["scope"] == "person"
    assert body["key"] == "date_dernier_echange" and body["required"] is False

    listing = (await client.get("/agencies/me/custom-fields", headers=headers)).json()
    assert [d["profile_section"] for d in listing if d["key"] == "date_dernier_echange"] == [
        SECTION_KEY
    ]
    # A PATCH moving another field INTO the long section takes the same column.
    other = await client.post(
        "/agencies/me/custom-fields",
        headers=headers,
        json={"key": "autre", "label": "Autre", "field_type": "text", "scope": "person"},
    )
    assert other.status_code == 201, other.text
    moved = await client.patch(
        f"/agencies/me/custom-fields/{other.json()['id']}",
        headers=headers,
        json={"profile_section": SECTION_KEY},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["profile_section"] == SECTION_KEY


async def test_company_surface_takes_a_21_char_section_too(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    await _create_section(client, headers, "company")
    created = await client.post(
        "/agencies/me/custom-fields",
        headers=headers,
        json={
            "label": "Dernier contact",
            "field_type": "text",
            "scope": "company",
            "profile_section": SECTION_KEY,
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["profile_section"] == SECTION_KEY


# --- (b) a 500 is a JSON envelope with a request id, logged with its traceback --------


async def test_unhandled_error_is_a_json_envelope_with_request_id_and_full_log(
    client: AsyncClient,  # holds the app's DB overrides for the whole test
    admin: Agent,
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def boom(self: AgenciesManager, agent: Agent) -> Any:
        raise RuntimeError("simulated unhandled failure")

    monkeypatch.setattr(AgenciesManager, "get_my_agency", boom)
    headers = {**agent_headers(admin), "Fly-Request-Id": "01M2K3SCZH4GCRY2C6V2KM5PPK-gru"}
    # raise_app_exceptions=False: observe the RESPONSE the client would get
    # (Starlette re-raises after sending it, by design).
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        with caplog.at_level(logging.ERROR, logger="src.core.exceptions"):
            response = await ac.get("/agencies/me", headers=headers)

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["x-request-id"] == "01M2K3SCZH4GCRY2C6V2KM5PPK-gru"
    assert response.json() == {
        "detail": "Internal server error.",
        "code": "internal_error",
        "params": {"request_id": "01M2K3SCZH4GCRY2C6V2KM5PPK-gru"},
    }
    # Server side: ONE record, the id + method + path, and the traceback.
    records = [r for r in caplog.records if "unhandled error" in r.getMessage()]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "request_id=01M2K3SCZH4GCRY2C6V2KM5PPK-gru" in message
    assert "GET /agencies/me" in message
    assert records[0].exc_info is not None
    assert "simulated unhandled failure" in caplog.text

    # No id sent: one is minted, echoed in both places, never empty.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.get("/agencies/me", headers=agent_headers(admin))
    assert response.status_code == 500
    minted = response.json()["params"]["request_id"]
    assert len(minted) == 32 and response.headers["x-request-id"] == minted
