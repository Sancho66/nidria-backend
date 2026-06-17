"""Step content (Feature 2, V1) — descending agency content on a TEMPLATE
step: a content_note (via the step PATCH) + attachments (Supabase storage,
generic primitive reused; NOT the case-scoped document table). Agency CRUD
only — the expat/external filtered read is V2.

Covers: content_note round-trip in the detail; attachment upload/list/
download (RFC 6266 header, shared with documents)/delete; storage↔DB
coherence (delete removes the file too); the journey.configure gate;
cross-agency / foreign-attachment scoping (404)."""

import pytest
import pytest_asyncio
from httpx import AsyncClient

from shared.models.agent import Agent
from shared.models.rbac import Role
from src.core import storage
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent


@pytest.fixture
def sc_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


@pytest_asyncio.fixture
async def member(admin: Agent, make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    """case.edit but NOT journey.configure."""
    return await make_agent(agency_id=admin.agency_id, role=system_roles["member"])


async def _template_step(client: AsyncClient, headers: dict[str, str]) -> tuple[str, str]:
    tid = (await client.post("/journeys", headers=headers, json={"name": "T"})).json()["id"]
    step = (
        await client.post(f"/journeys/{tid}/steps", headers=headers, json={"name": "S1"})
    ).json()
    return tid, step["id"]


def _file(name: str = "guide.pdf") -> dict[str, tuple[str, bytes]]:
    return {"file": (name, b"%PDF-1.4 step content")}


def _step_in_detail(detail: dict, sid: str) -> dict:
    return next(s for s in detail["steps"] if s["id"] == sid)


# --- content_note --------------------------------------------------------------------


async def test_content_note_round_trip(
    sc_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    tid, sid = await _template_step(sc_client, headers)
    # Set via the existing step PATCH.
    r = await sc_client.patch(
        f"/journeys/{tid}/steps/{sid}",
        headers=headers,
        json={"content_note": "Merci de fournir le justificatif de domicile."},
    )
    assert r.status_code == 200
    detail = (await sc_client.get(f"/journeys/{tid}", headers=headers)).json()
    step = _step_in_detail(detail, sid)
    assert step["content_note"] == "Merci de fournir le justificatif de domicile."
    assert step["attachments"] == []


# --- attachments: upload / list / download / delete + storage coherence --------------


async def test_attachment_full_cycle(
    sc_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    tid, sid = await _template_step(sc_client, headers)

    up = await sc_client.post(
        f"/journeys/{tid}/steps/{sid}/attachments", headers=headers, files=_file()
    )
    assert up.status_code == 201, up.text
    att = up.json()
    assert att["filename"] == "guide.pdf"

    # Listed + embedded in the template detail.
    listed = (
        await sc_client.get(f"/journeys/{tid}/steps/{sid}/attachments", headers=headers)
    ).json()
    assert [a["id"] for a in listed] == [att["id"]]
    detail = (await sc_client.get(f"/journeys/{tid}", headers=headers)).json()
    assert [a["id"] for a in _step_in_detail(detail, sid)["attachments"]] == [att["id"]]

    # The file is in storage.
    storage_paths = list(storage.mock_store.keys())
    assert any(att["id"] in p for p in storage_paths)

    # Download → 200 + bytes + RFC 6266 header (shared helper).
    dl = await sc_client.get(
        f"/journeys/{tid}/steps/{sid}/attachments/{att['id']}/download", headers=headers
    )
    assert dl.status_code == 200
    assert dl.content == b"%PDF-1.4 step content"
    dl.headers["content-disposition"].encode("latin-1")  # wire-encodable

    # Delete → row gone AND file gone (no orphan).
    rm = await sc_client.delete(
        f"/journeys/{tid}/steps/{sid}/attachments/{att['id']}", headers=headers
    )
    assert rm.status_code == 200
    assert (
        await sc_client.get(f"/journeys/{tid}/steps/{sid}/attachments", headers=headers)
    ).json() == []
    assert not any(att["id"] in p for p in storage.mock_store)  # file cleaned up


async def test_attachment_non_ascii_filename_download(
    sc_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """The shared RFC 6266 helper handles accents/curly apostrophe — the
    download must not 500 (same fix as documents, mutualized)."""
    headers = agent_headers(admin)
    tid, sid = await _template_step(sc_client, headers)
    att = (
        await sc_client.post(
            f"/journeys/{tid}/steps/{sid}/attachments",
            headers=headers,
            files=_file(name="Procédure d’accueil.pdf"),
        )
    ).json()
    dl = await sc_client.get(
        f"/journeys/{tid}/steps/{sid}/attachments/{att['id']}/download", headers=headers
    )
    assert dl.status_code == 200
    cd = dl.headers["content-disposition"]
    cd.encode("latin-1")
    assert "filename*=UTF-8''" in cd


# --- gate + scoping ------------------------------------------------------------------


async def test_attachment_gate_journey_configure(
    sc_client: AsyncClient, admin: Agent, member: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    tid, sid = await _template_step(sc_client, headers)
    denied = await sc_client.post(
        f"/journeys/{tid}/steps/{sid}/attachments",
        headers=agent_headers(member),  # lacks journey.configure
        files=_file(),
    )
    assert denied.status_code == 403


async def test_attachment_cross_agency_404(
    sc_client: AsyncClient,
    admin: Agent,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    tid, sid = await _template_step(sc_client, headers)
    att = (
        await sc_client.post(
            f"/journeys/{tid}/steps/{sid}/attachments", headers=headers, files=_file()
        )
    ).json()
    other_admin = await make_agent(role=system_roles["admin"])
    oh = agent_headers(other_admin)
    assert (
        await sc_client.get(f"/journeys/{tid}/steps/{sid}/attachments", headers=oh)
    ).status_code == 404
    assert (
        await sc_client.get(
            f"/journeys/{tid}/steps/{sid}/attachments/{att['id']}/download", headers=oh
        )
    ).status_code == 404
    assert (
        await sc_client.delete(f"/journeys/{tid}/steps/{sid}/attachments/{att['id']}", headers=oh)
    ).status_code == 404


async def test_attachment_foreign_id_404(
    sc_client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """An attachment of ANOTHER step (same template) → 404, no leak."""
    headers = agent_headers(admin)
    tid, s1 = await _template_step(sc_client, headers)
    s2 = (
        await sc_client.post(f"/journeys/{tid}/steps", headers=headers, json={"name": "S2"})
    ).json()["id"]
    att = (
        await sc_client.post(
            f"/journeys/{tid}/steps/{s2}/attachments", headers=headers, files=_file()
        )
    ).json()
    denied = await sc_client.delete(
        f"/journeys/{tid}/steps/{s1}/attachments/{att['id']}", headers=headers
    )
    assert denied.status_code == 404
