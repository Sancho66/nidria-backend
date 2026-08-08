"""Lot lecteur (08/08) — the 403 SWEEP, played with a REAL reader account.

The reader is the system `viewer` role (matrix: case.view only). This
file proves, one write per FAMILY, that a reader-eligible account is
refused every business write with a 403 — not a declarative list: every
request is actually played against the app. The enforcement dependency
resolves the binding BEFORE the handler, so unresolvable path ids still
answer 403 (the permission wall precedes any 404).

Also gravés here (arbitrages 07/08):
- `/views` writes stay OPEN to the reader — personal display prefs;
- `POST /agencies/me/onboarding/dismiss` is now GATED by case.view (the
  last business write without any permission): a viewer passes, an agent
  with an EMPTY role no longer does.
"""

import pytest
import pytest_asyncio
from httpx import AsyncClient

from shared.models.agent import Agent
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent

pytestmark = pytest.mark.usefixtures("rbac_baseline")

_ID = "00000000-0000-0000-0000-000000000001"


@pytest_asyncio.fixture
async def reader(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    """A REAL reader account: internal member, viewer role, reader seat."""
    return await make_agent(role=system_roles["viewer"], seat_type="reader")


# One write per family — (family, method, path). Bodies are irrelevant:
# the permission wall answers before any validation.
WRITE_SWEEP = [
    ("fiches-client", "POST", "/client-profiles"),
    ("fiches-client-notes", "POST", f"/client-profiles/{_ID}/notes"),
    ("fiches-societe", "POST", "/company-profiles"),
    ("dossiers", "POST", "/cases"),
    ("dossiers-bulk-tags", "POST", "/cases/bulk-action"),
    ("dossiers-bulk-delete", "POST", "/cases/bulk-delete"),
    ("personnes", "POST", f"/cases/{_ID}/persons"),
    ("contacts-externes", "POST", f"/cases/{_ID}/external-contacts"),
    ("notes", "POST", f"/cases/{_ID}/notes"),
    ("etapes", "PATCH", f"/cases/{_ID}/steps/{_ID}"),
    ("etapes-responsable", "PUT", f"/cases/{_ID}/steps/{_ID}/responsible"),
    ("etapes-validateur", "PUT", f"/cases/{_ID}/steps/{_ID}/validator"),
    ("parcours-affectation", "POST", f"/cases/{_ID}/journey"),
    ("documents", "POST", f"/cases/{_ID}/documents"),
    ("documents-validation", "PATCH", f"/cases/{_ID}/documents/{_ID}/validation"),
    ("documents-suppression", "DELETE", f"/cases/{_ID}/documents/{_ID}"),
    ("discussions", "POST", f"/cases/{_ID}/steps/{_ID}/comments"),
    ("couts", "POST", f"/cases/{_ID}/steps/{_ID}/costs"),
    ("couts-previsionnels", "POST", f"/journeys/{_ID}/steps/{_ID}/planned-costs"),
    ("imports", "POST", "/imports/client-profiles/preview"),
    ("imports-mappings", "POST", "/imports/mappings"),
    ("configs-agence", "PATCH", "/agencies/me"),
    ("configs-logo", "POST", "/agencies/me/logo"),
    ("champs", "POST", "/agencies/me/custom-fields"),
    ("champs-ordre", "PUT", "/agencies/me/custom-fields/order"),
    ("sections", "POST", "/agencies/me/profile-sections"),
    ("parcours", "POST", "/journeys"),
    ("parcours-etapes", "POST", f"/journeys/{_ID}/steps"),
    ("membres-invitations", "POST", "/agencies/me/invitations"),
    ("membres-desactivation", "POST", f"/agencies/me/members/{_ID}/deactivate"),
    ("membres-role", "PUT", f"/agencies/me/members/{_ID}/role"),
    ("membres-type-de-siege", "PUT", f"/agencies/me/members/{_ID}/seat-type"),
    ("roles", "POST", "/agencies/me/roles"),
    ("signatures", "POST", f"/cases/{_ID}/steps/{_ID}/signature-requests"),
    ("rappels", "POST", f"/cases/{_ID}/reminders"),
    ("rappels-approbation", "POST", f"/reminders/{_ID}/approve"),
    ("templates-messages", "POST", "/message-templates"),
    ("templates-documents", "POST", "/document-templates"),
    ("billing-checkout", "POST", "/billing/checkout"),
    ("billing-sieges", "POST", "/billing/seats/add"),
    ("billing-sieges-retrait", "POST", "/billing/seats/remove"),
    ("impersonation", "POST", f"/agencies/me/members/{_ID}/impersonate"),
]


@pytest.mark.parametrize(
    ("family", "method", "path"),
    WRITE_SWEEP,
    ids=[family for family, _, _ in WRITE_SWEEP],
)
async def test_reader_is_refused_every_business_write(
    client: AsyncClient,
    reader: Agent,
    agent_headers: AuthHeaders,
    family: str,
    method: str,
    path: str,
) -> None:
    response = await client.request(method, path, headers=agent_headers(reader), json={})
    assert response.status_code == 403, (
        f"{family}: {method} {path} answered {response.status_code} for a reader "
        f"(expected 403) — {response.text}"
    )


# The reads the cahier keeps OPEN: annuaires, fiches, dossiers, activité,
# dashboard, référentiels. List endpoints (no fixture data needed).
READ_SWEEP = [
    ("dossiers", "/cases"),
    ("fiches-client", "/client-profiles"),
    ("fiches-societe", "/company-profiles"),
    ("annuaire-membres", "/agencies/me/members"),
    ("roles-noms", "/agencies/me/roles"),
    ("agence", "/agencies/me"),
    ("dashboard", "/dashboard"),
    ("worklist", "/dashboard/worklist"),
    ("rappels", "/reminders"),
    ("champs", "/agencies/me/custom-fields"),
    ("sections", "/agencies/me/profile-sections"),
    ("parcours", "/journeys"),
    ("templates-messages", "/message-templates"),
    ("vues", "/views"),
    ("colonnes", "/cases/columns"),
]


@pytest.mark.parametrize(
    ("family", "path"),
    READ_SWEEP,
    ids=[family for family, _ in READ_SWEEP],
)
async def test_reader_keeps_every_read_open(
    client: AsyncClient,
    reader: Agent,
    agent_headers: AuthHeaders,
    family: str,
    path: str,
) -> None:
    response = await client.get(path, headers=agent_headers(reader))
    assert response.status_code == 200, (
        f"{family}: GET {path} answered {response.status_code} for a reader — {response.text}"
    )


async def test_views_writes_stay_open_to_the_reader(
    client: AsyncClient, reader: Agent, agent_headers: AuthHeaders
) -> None:
    """The ASSUMED exception (arbitrage 07/08): saved views are personal
    display preferences — a reader pins their own views."""
    created = await client.post(
        "/views",
        headers=agent_headers(reader),
        json={"name": "Mes dossiers", "filters": {}},
    )
    assert created.status_code == 201, created.text


async def test_onboarding_dismiss_is_now_gated(
    client: AsyncClient,
    reader: Agent,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
) -> None:
    """The matrix hole is closed (arbitrage 07/08): the dismiss carries
    the WEAKEST existing permission (case.view) — a viewer still passes
    (UX unchanged), an agent with an empty role no longer does."""
    no_perm = await make_agent()  # empty custom role — no permission at all
    refused = await client.post("/agencies/me/onboarding/dismiss", headers=agent_headers(no_perm))
    assert refused.status_code == 403, refused.text
    allowed = await client.post("/agencies/me/onboarding/dismiss", headers=agent_headers(reader))
    assert allowed.status_code == 200, allowed.text
